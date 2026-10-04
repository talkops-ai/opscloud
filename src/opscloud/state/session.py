"""Thread and session management using LangGraph SQLite checkpoint persistence.

Provides high-performance thread listing, batch checkpoint decoding with
JsonPlusSerializer, delta-channel-aware message counting, multi-tiered LRU caching,
and cloud environment metadata extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
    NotRequired,
    Self,
    TypedDict,
    cast,
)

from opscloud.middleware.goal_state_notice import is_internal_message
from opscloud.state.cloud_context import CloudEnvironmentContext, format_cloud_badge
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    import aiosqlite
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

logger = get_logger(__name__)

_aiosqlite_patched = False
_jsonplus_serializer: JsonPlusSerializer | None = None
_message_count_cache: dict[str, tuple[str | None, int]] = {}
_MAX_MESSAGE_COUNT_CACHE = 4096
_initial_prompt_cache: dict[str, tuple[str | None, str | None]] = {}
_MAX_INITIAL_PROMPT_CACHE = 4096
_cloud_badge_cache: dict[str, tuple[str | None, str | None]] = {}
_recent_threads_cache: dict[tuple[str | None, int], list[ThreadInfo]] = {}
_MAX_RECENT_THREADS_CACHE_KEYS = 16
_DEFAULT_SQLITE_TIMEOUT = 5.0
_SQLITE_MAX_VARIABLE_NUMBER = 500


def _patch_aiosqlite() -> None:
    """Patch aiosqlite.Connection with is_alive() if missing."""
    global _aiosqlite_patched
    if _aiosqlite_patched:
        return

    import aiosqlite as _aiosqlite

    if not hasattr(_aiosqlite.Connection, "is_alive"):
        def _is_alive(self: _aiosqlite.Connection) -> bool:
            return self._running and self._connection is not None

        _aiosqlite.Connection.is_alive = _is_alive  # type: ignore[attr-defined]

    _aiosqlite_patched = True


async def _drain_aiosqlite_worker(conn: Any) -> None:
    """Join the aiosqlite worker thread after its connection is closed."""
    worker = getattr(conn, "_thread", None)
    if worker is None or not worker.is_alive():
        return
    with contextlib.suppress(RuntimeError):
        await asyncio.to_thread(worker.join, 5.0)


async def _guard_sqlite_handle(conn: Any) -> None:
    """Apply connection safety settings."""
    with contextlib.suppress(Exception):
        await conn.execute("PRAGMA busy_timeout = 5000")


_db_path: Path | None = None


def get_db_path() -> Path:
    """Return the canonical path to the SQLite sessions database."""
    global _db_path
    if _db_path is not None:
        return _db_path
    from opscloud.config.paths import SESSIONS_DB_PATH

    SESSIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_path = SESSIONS_DB_PATH
    return _db_path


def set_db_path(path: Path) -> None:
    """Override database path (used by tests)."""
    global _db_path
    _db_path = path
    _message_count_cache.clear()
    _initial_prompt_cache.clear()
    _cloud_badge_cache.clear()
    _recent_threads_cache.clear()


@asynccontextmanager
async def _connect() -> AsyncGenerator[Any, None]:
    """Provide an asynchronous context manager connected to the sessions database."""
    import aiosqlite as _aiosqlite

    _patch_aiosqlite()
    conn: _aiosqlite.Connection | None = None
    try:
        async with _aiosqlite.connect(str(get_db_path()), timeout=30.0) as opened:
            conn = opened
            await _guard_sqlite_handle(opened)
            yield opened
    finally:
        if conn is not None:
            await _drain_aiosqlite_worker(conn)


class ThreadInfo(TypedDict):
    """Metadata for an OpsCloud execution thread."""

    thread_id: str
    agent_name: str | None
    updated_at: str | None
    created_at: NotRequired[str | None]
    git_branch: NotRequired[str | None]
    initial_prompt: NotRequired[str | None]
    message_count: NotRequired[int]
    latest_checkpoint_id: NotRequired[str | None]
    cwd: NotRequired[str | None]
    cloud_provider: NotRequired[str | None]
    cloud_badge: NotRequired[str | None]


class _CheckpointSummary(NamedTuple):
    """Structured operational facts extracted from a checkpoint payload."""

    message_count: int | None
    initial_prompt: str | None
    cloud_provider: str | None
    cloud_badge: str | None


def generate_thread_id() -> str:
    """Generate a chronological UUID7 string for thread identity."""
    try:
        from uuid_utils import uuid7
        return str(uuid7())
    except ImportError:
        import uuid
        return str(uuid.uuid4())


def format_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp to human readable date."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
        return dt.strftime("%b %d, %-I:%M%p").lower()
    except Exception:
        return ""


def format_relative_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp as a relative time string (e.g. 5m ago)."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
    except Exception:
        return ""

    delta = datetime.now(tz=dt.tzinfo) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(0, seconds)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


async def _table_exists(conn: Any, table: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    async with conn.execute(query, (table,)) as cursor:
        return await cursor.fetchone() is not None


_THREADS_LIST_INDEX = "idx_opscloud_threads_list"


async def _ensure_threads_list_index(conn: Any) -> None:
    try:
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {_THREADS_LIST_INDEX} ON checkpoints("
            "thread_id, "
            "json_extract(metadata, '$.updated_at'), "
            "checkpoint_id, "
            "json_extract(metadata, '$.agent_name'), "
            "json_extract(metadata, '$.git_branch'), "
            "json_extract(metadata, '$.cwd'))"
        )
        await conn.commit()
    except Exception:
        pass


def _get_jsonplus_serializer() -> JsonPlusSerializer:
    global _jsonplus_serializer
    if _jsonplus_serializer is not None:
        return _jsonplus_serializer
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    _jsonplus_serializer = JsonPlusSerializer()
    return _jsonplus_serializer


def _thread_freshness(thread: ThreadInfo) -> str | None:
    return thread.get("latest_checkpoint_id") or thread.get("updated_at")


def _cache_message_count(thread_id: str, freshness: str | None, count: int) -> None:
    if len(_message_count_cache) >= _MAX_MESSAGE_COUNT_CACHE:
        _message_count_cache.clear()
    _message_count_cache[thread_id] = (freshness, count)


def _cache_initial_prompt(thread_id: str, freshness: str | None, prompt: str | None) -> None:
    if len(_initial_prompt_cache) >= _MAX_INITIAL_PROMPT_CACHE:
        _initial_prompt_cache.clear()
    _initial_prompt_cache[thread_id] = (freshness, prompt)


def _cache_cloud_badge(thread_id: str, freshness: str | None, badge: str | None) -> None:
    if len(_cloud_badge_cache) >= _MAX_INITIAL_PROMPT_CACHE:
        _cloud_badge_cache.clear()
    _cloud_badge_cache[thread_id] = (freshness, badge)


def _visible_message_count(messages: list[object]) -> int:
    return sum(not is_internal_message(m) for m in messages)


def _checkpoint_messages(data: object) -> list[object] | None:
    if not isinstance(data, dict):
        return None
    channel_values = data.get("channel_values")
    if not isinstance(channel_values, dict):
        return None
    messages = channel_values.get("messages")
    if not isinstance(messages, list):
        return None
    return list(messages)


def _coerce_prompt_text(content: object) -> str | None:
    if isinstance(content, str):
        stripped = content.strip()
        return stripped if stripped else None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = " ".join(parts).strip()
        return joined if joined else None
    return None


def _initial_prompt_from_messages(messages: list[object]) -> str | None:
    for msg in messages:
        if is_internal_message(msg):
            continue
        if getattr(msg, "type", None) == "human":
            prompt = _coerce_prompt_text(getattr(msg, "content", None))
            if prompt:
                return prompt
        elif isinstance(msg, dict):
            role = msg.get("role")
            type_ = msg.get("type")
            if role in {"user", "human"} or type_ == "human":
                prompt = _coerce_prompt_text(msg.get("content"))
                if prompt:
                    return prompt
    return None


def _summarize_checkpoint(data: object) -> _CheckpointSummary:
    messages = _checkpoint_messages(data)
    count = _visible_message_count(messages) if messages is not None else None
    prompt = _initial_prompt_from_messages(messages or [])

    cloud_provider: str | None = None
    cloud_badge: str | None = None
    if isinstance(data, dict):
        cv = data.get("channel_values")
        if isinstance(cv, dict):
            provider_val = cv.get("_active_cloud_provider")
            if isinstance(provider_val, str):
                cloud_provider = provider_val
            ctx_val = cv.get("_cloud_context")
            if isinstance(ctx_val, dict):
                cloud_badge = format_cloud_badge(cast(CloudEnvironmentContext, ctx_val))
            elif cloud_provider:
                cloud_badge = f"[{cloud_provider.upper()}]"

    return _CheckpointSummary(
        message_count=count,
        initial_prompt=prompt,
        cloud_provider=cloud_provider,
        cloud_badge=cloud_badge,
    )


def _count_messages_from_deltas(deltas: list[Any]) -> int:
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
    from langgraph.types import Overwrite

    buffer: list[Any] = []
    needs_exact_fold = False
    for delta in deltas:
        if isinstance(delta, Overwrite):
            val = delta.value
            buffer = list(val) if isinstance(val, list) else []
            continue
        items = delta if isinstance(delta, list) else [delta]
        for item in items:
            if isinstance(item, RemoveMessage):
                if item.id == REMOVE_ALL_MESSAGES:
                    buffer = []
                else:
                    needs_exact_fold = True
                    break
            else:
                buffer.append(item)
        if needs_exact_fold:
            break

    if not needs_exact_fold:
        try:
            reduced = cast(list[Any], add_messages([], buffer))
            return _visible_message_count(reduced)
        except Exception:
            pass

    # Sequential fold fallback
    reduced_seq: list[Any] = []
    for delta in deltas:
        if isinstance(delta, Overwrite):
            val = delta.value
            reduced_seq = list(val) if isinstance(val, list) else []
            continue
        try:
            reduced_seq = cast(list[Any], add_messages(reduced_seq, delta))
        except Exception:
            pass
    return _visible_message_count(reduced_seq)


def _reduce_message_write_rows(
    rows: list[tuple[str, str | None, bytes | None]],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    deltas_by_thread: dict[str, list[Any]] = {}
    for tid, type_str, value_blob in rows:
        if not type_str or not value_blob:
            continue
        try:
            delta = serde.loads_typed((type_str, value_blob))
            deltas_by_thread.setdefault(tid, []).append(delta)
        except Exception:
            continue

    counts: dict[str, int] = {}
    for tid, deltas in deltas_by_thread.items():
        try:
            counts[tid] = _count_messages_from_deltas(deltas)
        except Exception:
            pass
    return counts


async def _load_latest_checkpoint_summaries_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, _CheckpointSummary]:
    if not thread_ids:
        return {}
    results: dict[str, _CheckpointSummary] = {}
    loop = asyncio.get_running_loop()

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT c.thread_id, c.type, c.checkpoint
            FROM checkpoints AS c
            JOIN (
                SELECT rowid AS rid,
                       ROW_NUMBER() OVER (
                           PARTITION BY thread_id ORDER BY checkpoint_id DESC
                       ) AS rn
                FROM checkpoints
                WHERE thread_id IN ({placeholders})
            ) AS ranked ON c.rowid = ranked.rid
            WHERE ranked.rn = 1
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        for tid, type_str, checkpoint_blob in rows:
            if not type_str or not checkpoint_blob:
                results[tid] = _CheckpointSummary(None, None, None, None)
                continue
            try:
                data = await loop.run_in_executor(
                    None, serde.loads_typed, (type_str, checkpoint_blob)
                )
                results[tid] = _summarize_checkpoint(data)
            except Exception:
                results[tid] = _CheckpointSummary(None, None, None, None)

    return results


async def _load_initial_prompts_from_writes_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, str | None]:
    if not thread_ids:
        return {}
    results: dict[str, str | None] = {}
    loop = asyncio.get_running_loop()

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT w.thread_id, w.type, w.value
            FROM writes AS w
            JOIN (
                SELECT rowid AS rid,
                       ROW_NUMBER() OVER (
                           PARTITION BY thread_id
                           ORDER BY checkpoint_id ASC, idx ASC
                       ) AS rn
                FROM writes
                WHERE thread_id IN ({placeholders}) AND channel = 'messages'
            ) AS ranked ON w.rowid = ranked.rid
            WHERE ranked.rn = 1
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        for tid, type_str, value_blob in rows:
            if not type_str or not value_blob:
                continue
            try:
                messages = await loop.run_in_executor(
                    None, serde.loads_typed, (type_str, value_blob)
                )
                if isinstance(messages, list):
                    prompt = _initial_prompt_from_messages(messages)
                    if prompt:
                        results[tid] = prompt
            except Exception:
                pass
    return results


async def _load_message_counts_from_writes_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    if not thread_ids:
        return {}
    loop = asyncio.get_running_loop()
    results: dict[str, int] = {}

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT thread_id, type, value
            FROM writes
            WHERE thread_id IN ({placeholders})
              AND checkpoint_ns = ''
              AND channel = 'messages'
            ORDER BY thread_id, checkpoint_id ASC, task_id ASC, idx ASC
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        chunk_counts = await loop.run_in_executor(
            None, _reduce_message_write_rows, list(rows), serde
        )
        results.update(chunk_counts)

    return results


async def _populate_checkpoint_fields(
    conn: Any,
    threads: list[ThreadInfo],
    *,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
) -> None:
    """Populate message count, initial prompt, and cloud context in threads."""
    serde = _get_jsonplus_serializer()
    uncached: list[ThreadInfo] = []

    for thread in threads:
        thread_id = thread["thread_id"]
        freshness = _thread_freshness(thread)
        needs_count = False
        needs_prompt = False

        if include_message_count:
            cached_cnt = _message_count_cache.get(thread_id)
            if cached_cnt is not None and cached_cnt[0] == freshness:
                thread["message_count"] = cached_cnt[1]
            else:
                needs_count = True

        if include_initial_prompt and "initial_prompt" not in thread:
            cached_p = _initial_prompt_cache.get(thread_id)
            if cached_p is not None and cached_p[0] == freshness:
                thread["initial_prompt"] = cached_p[1]
            else:
                needs_prompt = True

        cached_badge = _cloud_badge_cache.get(thread_id)
        if cached_badge is not None and cached_badge[0] == freshness:
            thread["cloud_badge"] = cached_badge[1]

        if needs_count or needs_prompt or "cloud_badge" not in thread:
            uncached.append(thread)

    if not uncached:
        return

    uncached_ids = [t["thread_id"] for t in uncached]
    batch_results = await _load_latest_checkpoint_summaries_batch(conn, uncached_ids, serde)
    prompt_results = await _load_initial_prompts_from_writes_batch(conn, uncached_ids, serde)

    needs_writes_count: list[str] = []
    for thread in uncached:
        tid = thread["thread_id"]
        freshness = _thread_freshness(thread)
        summary = batch_results.get(tid)

        if summary:
            if summary.cloud_badge:
                thread["cloud_badge"] = summary.cloud_badge
                _cache_cloud_badge(tid, freshness, summary.cloud_badge)
            if summary.cloud_provider:
                thread["cloud_provider"] = summary.cloud_provider

        if include_message_count and "message_count" not in thread:
            if summary and summary.message_count is not None:
                thread["message_count"] = summary.message_count
                _cache_message_count(tid, freshness, summary.message_count)
            else:
                needs_writes_count.append(tid)

        if include_initial_prompt and "initial_prompt" not in thread:
            prompt = prompt_results.get(tid) or (summary.initial_prompt if summary else None)
            thread["initial_prompt"] = prompt
            _cache_initial_prompt(tid, freshness, prompt)

    if needs_writes_count:
        writes_counts = await _load_message_counts_from_writes_batch(conn, needs_writes_count, serde)
        uncached_by_id = {t["thread_id"]: t for t in uncached}
        for tid in needs_writes_count:
            cnt = writes_counts.get(tid, 0)
            if tid in uncached_by_id:
                t = uncached_by_id[tid]
                t["message_count"] = cnt
                _cache_message_count(tid, _thread_freshness(t), cnt)


async def list_threads(
    agent_name: str | None = None,
    limit: int = 20,
    include_message_count: bool = True,
    sort_by: str = "updated",
    branch: str | None = None,
    cwd: str | None = None,
) -> list[ThreadInfo]:
    """List execution threads with parsed metadata, message counts, and initial prompts."""
    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return []

        await _ensure_threads_list_index(conn)

        order_col = "created_at" if sort_by == "created" else "updated_at"
        where_clauses: list[str] = []
        params_list: list[str | int] = []

        if agent_name:
            where_clauses.append("json_extract(metadata, '$.agent_name') = ?")
            params_list.append(agent_name)
        if branch:
            where_clauses.append("json_extract(metadata, '$.git_branch') = ?")
            params_list.append(branch)
        if cwd:
            where_clauses.append("json_extract(metadata, '$.cwd') = ?")
            params_list.append(cwd)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        query = f"""
            SELECT thread_id,
                   json_extract(metadata, '$.agent_name') as agent_name,
                   MAX(json_extract(metadata, '$.updated_at')) as updated_at,
                   MAX(checkpoint_id) as latest_checkpoint_id,
                   MIN(json_extract(metadata, '$.updated_at')) as created_at,
                   MAX(json_extract(metadata, '$.git_branch')) as git_branch,
                   MAX(json_extract(metadata, '$.cwd')) as cwd
            FROM checkpoints
            {where_sql}
            GROUP BY thread_id
            ORDER BY {order_col} DESC
            LIMIT ?
        """
        params: tuple = (*params_list, limit)

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            threads: list[ThreadInfo] = [
                ThreadInfo(
                    thread_id=r[0],
                    agent_name=r[1],
                    updated_at=r[2],
                    latest_checkpoint_id=r[3],
                    created_at=r[4],
                    git_branch=r[5],
                    cwd=r[6],
                )
                for r in rows
            ]

        if include_message_count and threads:
            await _populate_checkpoint_fields(
                conn,
                threads,
                include_message_count=True,
                include_initial_prompt=True,
            )

        return threads


async def prewarm_thread_message_counts(limit: int = 50) -> None:
    """Pre-warm caches in background for snappy TUI thread switching."""
    try:
        await list_threads(limit=limit, include_message_count=True)
    except Exception as exc:
        logger.debug("Failed prewarming thread message counts: %s", exc)


async def delete_thread(thread_id: str) -> bool:
    """Delete a thread and all associated checkpoints and writes."""
    deleted = False
    async with _connect() as conn:
        if await _table_exists(conn, "checkpoints"):
            cursor = await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
            )
            deleted = cursor.rowcount > 0
            if await _table_exists(conn, "writes"):
                await conn.execute(
                    "DELETE FROM writes WHERE thread_id = ?", (thread_id,)
                )
            await conn.commit()
            if deleted:
                _message_count_cache.pop(thread_id, None)
                _initial_prompt_cache.pop(thread_id, None)
                _cloud_badge_cache.pop(thread_id, None)
    return deleted


async def thread_exists(thread_id: str) -> bool:
    """Check if thread exists in checkpoints."""
    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return False
        async with conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def find_similar_threads(thread_id_prefix: str, limit: int = 3) -> list[str]:
    """Find threads starting with or matching prefix."""
    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return []
        query = """
            SELECT DISTINCT thread_id FROM checkpoints
            WHERE thread_id LIKE ?
            ORDER BY checkpoint_id DESC
            LIMIT ?
        """
        async with conn.execute(query, (f"{thread_id_prefix}%", limit)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def get_most_recent(
    agent_name: str | None = None,
    branch: str | None = None,
    cwd: str | None = None,
) -> ThreadInfo | None:
    """Return the most recently updated thread."""
    threads = await list_threads(
        agent_name=agent_name,
        limit=1,
        include_message_count=True,
        branch=branch,
        cwd=cwd,
    )
    return threads[0] if threads else None


from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class OpsCloudCheckpointer(AsyncSqliteSaver):
    """Custom SQLite checkpointer implementing LangGraph API extended capabilities.

    Adheres to the official LangChain custom checkpointer specification by implementing:
    - `adelete_for_runs`: deletes checkpoints and writes associated with specific run IDs,
      enabling the multitask_strategy='rollback' capability.
    - `acopy_thread`: duplicates thread state, checkpoints, and writes for thread forking.
    - `aprune`: prunes thread checkpoint history ('keep_latest' or 'delete_all') to keep
      storage bounded for long-lived threads.
    """

    @classmethod
    @asynccontextmanager
    async def from_conn_string(
        cls: type[Self], conn_string: str
    ) -> AsyncIterator[Self]:
        """Create a new OpsCloudCheckpointer instance from a connection string."""
        import aiosqlite

        async with aiosqlite.connect(conn_string) as conn:
            yield cls(conn)

    async def adelete_for_runs(self, run_ids: Sequence[str] | Iterable[str]) -> None:
        """Asynchronously delete all checkpoints and writes associated with the given run IDs.

        Enables the LangGraph API multitask_strategy='rollback' to clean up
        checkpoints and intermediate writes when a run is cancelled.
        """
        run_id_list = [r for r in run_ids if r]
        if not run_id_list:
            return
        await self.setup()
        async with self.lock, self.conn.cursor() as cur:
            for r_id in run_id_list:
                await cur.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints "
                    "WHERE json_extract(CAST(metadata AS TEXT), '$.run_id') = ?",
                    (r_id,),
                )
                cps = await cur.fetchall()
                for tid, c_ns, cid in cps:
                    await cur.execute(
                        "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                        (tid, c_ns, cid),
                    )
                    await cur.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                        (tid, c_ns, cid),
                    )
            await self.conn.commit()

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Asynchronously copy all checkpoints and writes from one thread to another.

        Supports LangGraph API POST /threads/<id>/copy with fast SQLite bulk duplication.
        """
        source_id = source_thread_id
        target_id = target_thread_id
        await self.setup()
        async with self.lock, self.conn.cursor() as cur:
            await cur.execute(
                "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
                "FROM checkpoints WHERE thread_id = ?",
                (source_id,),
            )
            rows = await cur.fetchall()
            for checkpoint_ns, checkpoint_id, parent_checkpoint_id, cp_type, checkpoint_blob, metadata_blob in rows:
                updated_metadata_blob = metadata_blob
                if metadata_blob:
                    try:
                        meta_dict = json.loads(metadata_blob.decode("utf-8", "ignore"))
                        if "thread_id" in meta_dict:
                            meta_dict["thread_id"] = target_id
                        updated_metadata_blob = json.dumps(meta_dict, ensure_ascii=False).encode("utf-8", "ignore")
                    except Exception:
                        pass
                await cur.execute(
                    "INSERT OR REPLACE INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (target_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, cp_type, checkpoint_blob, updated_metadata_blob),
                )
            await cur.execute(
                "SELECT checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value "
                "FROM writes WHERE thread_id = ?",
                (source_id,),
            )
            write_rows = await cur.fetchall()
            for checkpoint_ns, checkpoint_id, task_id, idx, channel, w_type, value_blob in write_rows:
                await cur.execute(
                    "INSERT OR REPLACE INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, w_type, value_blob),
                )
            await self.conn.commit()

    async def aprune(
        self, thread_ids: Sequence[str], *, strategy: str = "keep_latest"
    ) -> None:
        """Asynchronously prune checkpoints for the given threads according to the strategy.

        Supports 'delete_all' / 'delete' and 'keep_latest' thread history pruning.
        """
        if strategy in ("delete_all", "delete"):
            for tid in thread_ids:
                await self.adelete_thread(tid)
            return
        if strategy != "keep_latest":
            raise ValueError(f"Unknown prune strategy: {strategy}")

        await self.setup()
        async with self.lock, self.conn.cursor() as cur:
            for tid in thread_ids:
                await cur.execute(
                    "SELECT checkpoint_ns, MAX(checkpoint_id) FROM checkpoints "
                    "WHERE thread_id = ? GROUP BY checkpoint_ns",
                    (tid,),
                )
                latest_rows = await cur.fetchall()
                for checkpoint_ns, latest_cid in latest_rows:
                    if latest_cid is None:
                        continue
                    await cur.execute(
                        "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id != ?",
                        (tid, checkpoint_ns, latest_cid),
                    )
                    await cur.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id != ?",
                        (tid, checkpoint_ns, latest_cid),
                    )
            await self.conn.commit()


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[OpsCloudCheckpointer, None]:
    """Provide an OpsCloudCheckpointer connected to the sessions database."""
    _patch_aiosqlite()
    db_path = get_db_path()
    async with OpsCloudCheckpointer.from_conn_string(str(db_path)) as saver:
        await _guard_sqlite_handle(saver.conn)
        yield saver


class SessionManager:
    """High-level facade for thread session lifecycle in OpsCloud."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is not None:
            set_db_path(db_path)
        self.db_path = get_db_path()

    @staticmethod
    def get_db_path() -> Path:
        return get_db_path()

    @staticmethod
    async def list_threads(
        limit: int = 50,
        agent_name: str | None = None,
        include_message_count: bool = True,
    ) -> list[ThreadInfo]:
        return await list_threads(
            limit=limit,
            agent_name=agent_name,
            include_message_count=include_message_count,
        )

    @staticmethod
    async def delete_thread(thread_id: str) -> bool:
        return await delete_thread(thread_id)

    @staticmethod
    async def get_most_recent(agent_name: str | None = None) -> ThreadInfo | None:
        return await get_most_recent(agent_name=agent_name)

    @staticmethod
    async def thread_exists(thread_id: str) -> bool:
        return await thread_exists(thread_id)

    @staticmethod
    async def find_similar_threads(prefix: str, limit: int = 3) -> list[str]:
        return await find_similar_threads(prefix, limit=limit)

    @staticmethod
    def get_checkpointer() -> Any:
        return get_checkpointer()


__all__ = [
    "OpsCloudCheckpointer",
    "SessionManager",
    "ThreadInfo",
    "delete_thread",
    "find_similar_threads",
    "format_relative_timestamp",
    "format_timestamp",
    "generate_thread_id",
    "get_checkpointer",
    "get_db_path",
    "get_most_recent",
    "list_threads",
    "prewarm_thread_message_counts",
    "set_db_path",
    "thread_exists",
]
