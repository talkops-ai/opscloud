"""Approval-mode state shared by clients, server, and middleware in OpsCloud.

Aligned with official dcode contracts and tailored for OpsCloud terminal operations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Mapping
import contextlib
from contextlib import contextmanager
from enum import StrEnum
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, TypedDict

from filelock import FileLock, Timeout

from opscloud.config.paths import STATE_DIR
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

APPROVAL_MODE_NAMESPACE: tuple[str, str] = ("opscloud", "approval_mode")
"""Store namespace for per-thread approval-mode control records."""

AUTO_NOTICE_VERSION = "2026-08-23"


class ApprovalMode(StrEnum):
    """Tool-approval policy selected for an interactive thread.

    - MANUAL: Prompt human for confirmation on risky or mutating actions (safe default).
    - AUTO: Classic agent LLM classifier decides (uses primary reasoning model).
    - SMART: Jev System One classifier decides (ultra-fast <100ms, calibrated risk probabilities).
    """

    MANUAL = "manual"
    AUTO = "auto"
    SMART = "smart"

    # Compatibility alias
    ALWAYS = "manual"

    @classmethod
    def from_str(cls, value: str | None) -> ApprovalMode:
        """Parse string to ApprovalMode, failing closed to MANUAL."""
        return coerce_approval_mode(value)


class ApprovalModePayload(TypedDict):
    """Stored approval-mode control payload."""

    mode: str


def coerce_approval_mode(value: object) -> ApprovalMode:
    """Return a validated mode, failing closed to `MANUAL`."""
    if isinstance(value, bool):
        return ApprovalMode.AUTO if value else ApprovalMode.MANUAL
    if value is None:
        return ApprovalMode.MANUAL
    if isinstance(value, ApprovalMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"manual", "m", "always"}:
            return ApprovalMode.MANUAL
        if normalized in {"auto", "a"}:
            return ApprovalMode.AUTO
        if normalized in {"smart", "s", "jev"}:
            return ApprovalMode.SMART
    return ApprovalMode.MANUAL


def approval_mode_payload(
    *,
    mode: ApprovalMode | str | None = None,
    auto_approve: bool | None = None,
) -> ApprovalModePayload:
    """Return the stored approval-mode payload.

    Args:
        mode: Explicit approval mode.
        auto_approve: Compatibility input for callers using previous Boolean API.
            `True` maps to `auto`, and `False` maps to `manual`.

    Returns:
        JSON-serializable store value.

    Raises:
        ValueError: If neither or both inputs are supplied, or `mode` is invalid.
    """
    if (mode is None) == (auto_approve is None):
        msg = "Provide exactly one of mode or auto_approve"
        raise ValueError(msg)
    if auto_approve is not None:
        resolved = ApprovalMode.AUTO if auto_approve else ApprovalMode.MANUAL
    else:
        resolved = coerce_approval_mode(mode)
    return {"mode": resolved.value}


def next_approval_mode(
    current: ApprovalMode | str | object,
    *,
    auto_eligible: bool = True,
    smart_eligible: bool = True,
) -> ApprovalMode:
    """Cycle to the next approval mode (Manual → Auto → Smart → Manual).

    Used by the Textual TUI (`Shift+Tab` switcher) and CLI interactive cycles.
    """
    mode = current if isinstance(current, ApprovalMode) else coerce_approval_mode(current)

    if mode == ApprovalMode.MANUAL:
        if auto_eligible:
            return ApprovalMode.AUTO
        if smart_eligible:
            return ApprovalMode.SMART
        return ApprovalMode.MANUAL

    if mode == ApprovalMode.AUTO:
        if smart_eligible:
            return ApprovalMode.SMART
        return ApprovalMode.MANUAL

    if mode == ApprovalMode.SMART:
        return ApprovalMode.MANUAL

    return ApprovalMode.MANUAL


def approval_mode_key(thread_id: str) -> str:
    """Derive the canonical Store key for a thread's approval mode."""
    return f"thread:{thread_id}:mode"


def _item_value(item: object) -> object:
    """Extract a store item's value."""
    if isinstance(item, Mapping):
        return item.get("value")
    return getattr(item, "value", None)


def _approval_mode_from_item(item: object) -> ApprovalMode | None:
    """Extract a validated approval mode from a Store item."""
    if item is None:
        logger.debug("Approval-mode store item is missing")
        return None

    value = _item_value(item)
    raw_mode = value.get("mode") if isinstance(value, Mapping) else None
    if isinstance(raw_mode, str):
        try:
            return ApprovalMode(raw_mode)
        except ValueError:
            pass

    logger.warning("Approval-mode store item has invalid contents")
    return None


def read_approval_mode_from_store(
    store: object, key: str | None
) -> ApprovalMode | None:
    """Read a live approval mode from the server-side LangGraph Store.

    Args:
        store: `request.runtime.store` from the graph server.
        key: Store key produced by `approval_mode_key`.

    Returns:
        A validated mode, or `None` when the record cannot be trusted. Callers
        must interpret `None` as `manual`.
    """
    if store is None:
        logger.debug("Approval-mode store is unavailable")
        return None
    if not isinstance(key, str) or not key:
        logger.debug("Approval-mode store key is missing or invalid")
        return None

    get = getattr(store, "get", None)
    if get is None:
        logger.debug("Approval-mode store does not expose get()")
        return None

    try:
        item = get(APPROVAL_MODE_NAMESPACE, key)
    except asyncio.InvalidStateError as exc:
        logger.debug(
            "Synchronous store.get() rejected on event loop (%s): %s",
            type(store).__name__,
            exc,
        )
        return None
    except Exception:
        logger.warning("Could not read approval-mode store item", exc_info=True)
        return None
    return _approval_mode_from_item(item)


async def aread_approval_mode_from_store(
    store: object, key: str | None
) -> ApprovalMode | None:
    """Asynchronously read a live approval mode from a LangGraph Store.

    Args:
        store: `request.runtime.store` from the graph server.
        key: Store key produced by `approval_mode_key`.

    Returns:
        A validated mode, or `None` when the record cannot be trusted. Callers
        must interpret `None` as `manual`.
    """
    if store is None:
        logger.debug("Approval-mode store is unavailable")
        return None
    if not isinstance(key, str) or not key:
        logger.debug("Approval-mode store key is missing or invalid")
        return None

    aget = getattr(store, "aget", None)
    get = getattr(store, "get", None)
    try:
        if callable(aget):
            result = aget(APPROVAL_MODE_NAMESPACE, key)
            item = await result if inspect.isawaitable(result) else result
        elif callable(get):
            item = get(APPROVAL_MODE_NAMESPACE, key)
        else:
            logger.debug("Approval-mode store does not expose get() or aget()")
            return None
    except asyncio.InvalidStateError as exc:
        logger.debug("Asynchronous store access rejected: %s", exc)
        return None
    except Exception:
        logger.warning("Could not read approval-mode store item", exc_info=True)
        return None
    return _approval_mode_from_item(item)


async def awrite_approval_mode(
    agent: object,
    thread_id: str,
    *,
    mode: ApprovalMode | str | None = None,
    auto_approve: bool | None = None,
) -> str | None:
    """Persist approval mode through an agent's remote store client or attached store.

    Args:
        agent: Agent object (remote agent or client exposing `aput_store_item`, or object with `.store`).
        thread_id: LangGraph thread id for the active session.
        mode: Explicit approval mode.
        auto_approve: Compatibility input for the previous Boolean API.

    Returns:
        Store key written, or `None` when the agent has no store writer.
    """
    key = approval_mode_key(thread_id)
    try:
        payload = approval_mode_payload(mode=mode, auto_approve=auto_approve)
    except Exception:
        logger.debug("Failed to construct approval mode payload", exc_info=True)
        return None

    # Synchronize in-memory fallback state
    resolved = coerce_approval_mode(payload.get("mode"))
    set_approval_mode(thread_id, resolved)

    written = False

    # 1. Agent exposes direct aput_store_item (e.g. RemoteAgent)
    put = getattr(agent, "aput_store_item", None)
    if callable(put):
        try:
            result = put(APPROVAL_MODE_NAMESPACE, key, payload)
            if inspect.isawaitable(result):
                await result
            written = True
        except Exception:
            logger.debug("Failed to write approval mode via agent.aput_store_item", exc_info=True)

    # 2. Agent exposes store or agent itself is a store
    if not written:
        store = getattr(agent, "store", None)
        if store is None and any(callable(getattr(agent, attr, None)) for attr in ("put_item", "aput", "put")):
            store = agent

        if store is not None:
            try:
                put_item_fn = getattr(store, "put_item", None)
                aput_fn = getattr(store, "aput", None)
                put_fn = getattr(store, "put", None)

                if callable(put_item_fn):
                    res = put_item_fn(APPROVAL_MODE_NAMESPACE, key, payload, index=False)
                    if inspect.isawaitable(res):
                        await res
                    written = True
                elif callable(aput_fn):
                    res = aput_fn(APPROVAL_MODE_NAMESPACE, key, payload)
                    if inspect.isawaitable(res):
                        await res
                    written = True
                elif callable(put_fn):
                    res = put_fn(APPROVAL_MODE_NAMESPACE, key, payload)
                    if inspect.isawaitable(res):
                        await res
                    written = True
            except Exception:
                logger.debug("Failed to write approval mode via agent.store", exc_info=True)

    return key


_IN_MEMORY_MODES: dict[str, ApprovalMode] = {}


def get_approval_mode(thread_id: str | None = None) -> ApprovalMode:
    """Retrieve current approval mode for thread, failing closed to MANUAL."""
    if not thread_id:
        return ApprovalMode.MANUAL
    return _IN_MEMORY_MODES.get(thread_id, ApprovalMode.MANUAL)


def set_approval_mode(thread_id: str, mode: ApprovalMode | str) -> ApprovalMode:
    """Persist updated approval mode for thread in memory."""
    resolved = coerce_approval_mode(mode)
    _IN_MEMORY_MODES[thread_id] = resolved
    return resolved


@contextmanager
def approval_mode_scope(thread_id: str, mode: ApprovalMode | str) -> Generator[ApprovalMode, None, None]:
    """Temporarily override approval mode within a context manager block."""
    prev = get_approval_mode(thread_id)
    new_mode = set_approval_mode(thread_id, mode)
    try:
        yield new_mode
    finally:
        set_approval_mode(thread_id, prev)


_APPROVAL_STATE_LOCK_TIMEOUT_SECONDS = 5.0
"""Longest a save waits for the shared install-local approval-state lock."""

_APPROVAL_STATE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_APPROVAL_STATE_THREAD_LOCKS_GUARD = threading.Lock()


def approval_state_path() -> Path:
    """Return the installation-local approval state file path.

    Returns:
        Path under the private opscloud state directory.
    """
    return STATE_DIR / "approval.json"


def _approval_state_lock_path(path: Path) -> Path:
    """Return the sibling lock file path that serializes approval-state saves."""
    return path.with_name(f"{path.name}.lock")


def _approval_state_thread_lock(path: Path) -> threading.Lock:
    """Return the process-local mutation lock for an approval-state path."""
    key = str(path)
    with _APPROVAL_STATE_THREAD_LOCKS_GUARD:
        lock = _APPROVAL_STATE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _APPROVAL_STATE_THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _approval_state_lock(path: Path) -> Generator[None, None, None]:
    """Serialize read-merge-write updates to install-local approval state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(0o700)
    file_lock = FileLock(
        str(_approval_state_lock_path(path)),
        timeout=_APPROVAL_STATE_LOCK_TIMEOUT_SECONDS,
        thread_local=False,
    )
    with _approval_state_thread_lock(path), file_lock:
        yield


def _load_approval_state(path: Path) -> dict[str, object]:
    """Load the install-local approval state file, or an empty dict."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "Ignoring unreadable or corrupt approval state at %s; callers fail "
            "closed and the file will be overwritten on the next save",
            path,
            exc_info=True,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Ignoring non-object approval state at %s; callers fail closed and "
            "the file will be overwritten on the next save",
            path,
        )
        return {}
    return data


def _write_approval_state(
    path: Path,
    payload: Mapping[str, object],
    *,
    failure_label: str,
) -> bool:
    """Atomically write install-local approval state."""
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            path.parent.chmod(0o700)
        fd, raw_tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp_path = Path(raw_tmp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        if os.name != "nt":
            tmp_path.chmod(0o600)
        tmp_path.replace(path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        logger.warning("Could not persist %s", failure_label, exc_info=True)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        return False
    return True


def _merge_approval_state(
    path: Path,
    updates: Mapping[str, object],
    *,
    failure_label: str,
) -> bool:
    """Load, merge, and write install-local approval state under a lock."""
    try:
        with _approval_state_lock(path):
            payload = {
                **_load_approval_state(path),
                **updates,
                "version": 1,
            }
            return _write_approval_state(
                path,
                payload,
                failure_label=failure_label,
            )
    except Timeout:
        logger.warning(
            "Timed out waiting to persist %s",
            failure_label,
            exc_info=True,
        )
        return False
    except OSError:
        logger.warning(
            "Could not lock approval state for %s", failure_label, exc_info=True
        )
        return False


def has_auto_mode_notice(path: Path | None = None) -> bool:
    """Return whether the current Auto first-enable notice was already shown."""
    target = path or approval_state_path()
    data = _load_approval_state(target)
    return (
        data.get("auto_notice_shown") is True
        and data.get("auto_notice_version") == AUTO_NOTICE_VERSION
    )


def save_auto_mode_notice(path: Path | None = None) -> bool:
    """Persist that the Auto first-enable notice was shown."""
    target = path or approval_state_path()
    return _merge_approval_state(
        target,
        {
            "auto_notice_version": AUTO_NOTICE_VERSION,
            "auto_notice_shown": True,
        },
        failure_label="Auto mode notice",
    )


__all__ = [
    "APPROVAL_MODE_NAMESPACE",
    "AUTO_NOTICE_VERSION",
    "ApprovalMode",
    "ApprovalModePayload",
    "approval_mode_key",
    "approval_mode_payload",
    "approval_mode_scope",
    "approval_state_path",
    "aread_approval_mode_from_store",
    "awrite_approval_mode",
    "coerce_approval_mode",
    "get_approval_mode",
    "has_auto_mode_notice",
    "next_approval_mode",
    "read_approval_mode_from_store",
    "save_auto_mode_notice",
    "set_approval_mode",
]

