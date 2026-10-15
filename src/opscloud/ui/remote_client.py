"""Remote agent client — wrapper around LangGraph's RemoteGraph.

Delegates streaming, state management, and SSE handling to
langgraph.pregel.remote.RemoteGraph. Converts streamed message dicts into
LangChain message objects for TextualAdapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any, Callable

import httpx

from opscloud.utils.logger import get_logger

from dataclasses import dataclass, field

logger = get_logger(__name__)

_RUN_CANCEL_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class CancellationResult:
    """Structured outcome of run cancellation on a thread (DV-09)."""

    success: bool
    cancelled_run_ids: tuple[str, ...] = ()
    failed_run_ids: tuple[str, ...] = ()
    terminal_statuses: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def normalize_thread_id(thread_id: str | None) -> str:
    """Ensure thread_id is a valid UUID string for LangGraph Server API.

    If an arbitrary string or non-UUID thread identifier is passed (such as
    'local_session', 'my_session', etc.), convert it deterministically to
    a valid UUIDv5.
    """
    if not thread_id:
        import uuid
        return str(uuid.uuid4())
    try:
        import uuid
        uuid.UUID(thread_id)
        return thread_id
    except ValueError:
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, thread_id))


def _require_thread_id(config: Mapping[str, Any] | None) -> str:
    thread_id = (config or {}).get("configurable", {}).get("thread_id")
    if not thread_id:
        msg = "thread_id is required in config.configurable"
        raise ValueError(msg)
    return str(thread_id)


def agent_error_type(exc: BaseException) -> str:
    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        err_type = payload.get("error")
        if isinstance(err_type, str) and err_type:
            return err_type
    return type(exc).__name__


def format_agent_exception(exc: BaseException) -> str:
    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        err_type = agent_error_type(exc)
        message = payload.get("message")
        if isinstance(message, str) and message:
            return f"{err_type}: {message}"
        return err_type
    text = str(exc)
    return text or type(exc).__name__

_KNOWN_STREAM_MODES = {"messages", "updates", "custom", "values", "debug"}


def _parse_stream_item(raw_item: Any) -> tuple[tuple[str, ...], str, Any] | None:
    """Parse a stream chunk into (namespace, mode, data) matching Pregel and RemoteGraph format."""
    # Check if raw_item is a StreamPart from langgraph_sdk
    if hasattr(raw_item, "event") and hasattr(raw_item, "data"):
        event = str(raw_item.event)
        data = raw_item.data
        if "|" in event:
            mode, ns_ = event.split("|", 1)
            ns = tuple(ns_.split("|"))
        else:
            mode = event
            ns = ()
        return ns, mode, data

    if not isinstance(raw_item, tuple):
        return None
    if len(raw_item) == 3:
        ns, mode, data = raw_item
        ns_tuple = tuple(ns) if isinstance(ns, (list, tuple)) else ()
        return ns_tuple, str(mode), data
    if len(raw_item) == 2:
        mode, data = raw_item
        return (), str(mode), data
    return None


class RemoteAgent:
    """Client that talks to a LangGraph server over HTTP+SSE.

    Wraps `langgraph.pregel.remote.RemoteGraph` which handles SSE parsing,
    stream-mode negotiation (`messages-tuple`), namespace extraction, and
    interrupt detection. This class adds streamed message-object conversion for
    the Textual adapter and thread-ID normalization. State snapshots are
    returned as provided by the server.
    """

    def __init__(
        self,
        url: str,
        *,
        graph_name: str = "agent",
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._graph_name = graph_name
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._graph: Any = None
        self._active_run_id: str | None = None

    async def aclose(self) -> None:
        """Explicitly close SDK client and underlying httpx connection pool (DV-10)."""
        graph = self._graph
        self._graph = None
        if graph is not None and hasattr(graph, "_validate_client"):
            try:
                client = graph._validate_client()
                if hasattr(client, "http") and hasattr(client.http, "aclose"):
                    await client.http.aclose()
            except Exception:
                pass

    def _get_graph(self) -> Any:
        if self._graph is None:
            from langgraph_sdk import get_client
            from langgraph.pregel.remote import RemoteGraph

            # Timeout tuple conforming to langgraph_sdk.client.TimeoutTypes:
            # (connect, read, write, pool) in seconds
            client = get_client(
                url=self._url,
                api_key=self._api_key,
                headers=self._headers if self._headers else None,
                timeout=(10.0, 600.0, 300.0, 10.0),
            )
            self._graph = RemoteGraph(
                self._graph_name,
                client=client,
            )
        return self._graph

    async def astream(
        self,
        input: dict | Any,
        *,
        stream_mode: list[str] | None = None,
        subgraphs: bool = False,
        config: dict[str, Any] | None = None,
        context: Any | None = None,
        durability: str | None = None,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        from langchain_core.messages import BaseMessage

        _require_thread_id(config)

        prepared_config = _prepare_config(config)

        # In LangGraph 0.6.0+, runs reject specifying both `configurable` (with non-thread_id keys)
        # and `context`. If context is provided, ensure that configurable contains ONLY thread_id,
        # and any extra keys are forwarded via context payload.
        payload = dict(context) if isinstance(context, Mapping) else (dict(context.__dict__) if hasattr(context, "__dict__") else {})
        if "configurable" in prepared_config:
            conf = dict(prepared_config["configurable"])
            thread_id = conf.pop("thread_id", None)
            if thread_id and "thread_id" not in payload:
                payload["thread_id"] = thread_id
            for k, v in conf.items():
                if k not in payload:
                    payload[k] = v
            prepared_config["configurable"] = {"thread_id": thread_id} if thread_id else {}

        if payload.get("thread_id") and not payload.get("approval_mode_key"):
            from opscloud.security.approval_mode import approval_mode_key
            payload["approval_mode_key"] = approval_mode_key(str(payload["thread_id"]))

        stream_context = payload if payload else context
        dropped_count = 0

        active_tid = payload.get("thread_id") or (
            prepared_config.get("configurable", {}).get("thread_id")
            if isinstance(prepared_config.get("configurable"), dict)
            else None
        )
        if active_tid:
            from opscloud.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID

            ACTIVE_SESSION_THREAD_ID.set(str(active_tid))

        # DV-09: Register on_run_created callback to capture active_run_id immediately upon creation
        def _on_run_created(run_meta: Any) -> None:
            rid = getattr(run_meta, "run_id", None) or (run_meta.get("run_id") if isinstance(run_meta, dict) else None)
            if rid:
                self._active_run_id = str(rid)
                logger.debug("Captured active run_id on creation: %s", self._active_run_id)

        stream_kwargs: dict[str, Any] = {
            "on_run_created": _on_run_created,
        }
        if durability is not None:
            stream_kwargs["durability"] = durability

        graph = self._get_graph()
        try:
            async for raw_item in graph.astream(
                input,
                stream_mode=stream_mode or ["messages", "updates"],
                subgraphs=subgraphs,
                config=prepared_config,
                context=stream_context,
                **stream_kwargs,
            ):
                parsed = _parse_stream_item(raw_item)
                if parsed is None:
                    continue
                ns, mode, data = parsed

                logger.debug("RemoteGraph event ns=%s mode=%s data_type=%s", ns, mode, type(data).__name__)

                if mode == "messages":
                    msg_dict, meta = data if isinstance(data, tuple) else (data, {})
                    if isinstance(meta, dict) and meta.get("run_id"):
                        self._active_run_id = str(meta["run_id"])
                    if isinstance(msg_dict, dict):
                        msg_obj = _convert_message_data(msg_dict)
                        if msg_obj is not None:
                            yield (ns, "messages", (msg_obj, meta or {}))
                        else:
                            dropped_count += 1
                    elif isinstance(msg_dict, BaseMessage):
                        yield (ns, "messages", (msg_dict, meta or {}))
                    else:
                        logger.warning("Unexpected message data type in stream: %s", type(msg_dict).__name__)
                    continue

                if mode == "updates" and isinstance(data, dict):
                    update_data = data
                    if "__interrupt__" in data:
                        update_data = {
                            **data,
                            "__interrupt__": _convert_interrupts(data["__interrupt__"]),
                        }
                    yield (ns, "updates", update_data)
                    continue

                yield (ns, mode, data)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            old_graph = self._graph
            self._graph = None  # Reset client connection pool
            if old_graph is not None and hasattr(old_graph, "_validate_client"):
                try:
                    c = old_graph._validate_client()
                    if hasattr(c, "http") and hasattr(c.http, "aclose"):
                        await c.http.aclose()
                except Exception:
                    pass
            logger.error(
                "Remote stream transport/timeout failure (%s: %s). Input re-submission suppressed to prevent duplicate tool execution.",
                type(exc).__name__,
                exc,
            )
            raise

        if dropped_count:
            logger.warning("Dropped %d message(s) during stream due to conversion failures", dropped_count)

    async def aget_state(self, config: dict[str, Any]) -> Any:
        from langgraph_sdk.errors import NotFoundError

        thread_id = _require_thread_id(config)
        graph = self._get_graph()
        try:
            return await graph.aget_state(_prepare_config(config))
        except NotFoundError:
            logger.debug("Thread %s not found on server", thread_id)
            return None
        except TypeError as e:
            if "subscriptable" in str(e).lower():
                logger.debug("Thread %s has no checkpoint yet", thread_id)
                return None
            raise

    async def acancel_active_runs(self, config: dict[str, Any]) -> CancellationResult:
        """Cancel any in-flight runs on the thread, keeping active_run_id until terminal state confirmed (DV-09)."""
        thread_id = _require_thread_id(config)
        graph = self._get_graph()
        try:
            client = graph._validate_client()
        except Exception as exc:
            logger.debug("Failed to get SDK client to cancel runs for thread %s", thread_id, exc_info=True)
            return CancellationResult(success=False, cancelled_run_ids=(), failed_run_ids=(), error=str(exc))

        run_ids: set[str] = set()
        if self._active_run_id:
            run_ids.add(self._active_run_id)

        for status in ("running", "pending"):
            try:
                runs = await client.runs.list(thread_id, status=status, limit=10)
                for run in runs:
                    r_status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
                    if r_status is not None and r_status != status:
                        continue
                    rid = run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", None)
                    if rid:
                        run_ids.add(rid)
            except Exception:
                logger.debug("Failed to list %s runs for thread %s", status, thread_id, exc_info=True)

        if not run_ids:
            try:
                runs = await client.runs.list(thread_id)
                for r in runs:
                    status = r.get("status") if isinstance(r, dict) else getattr(r, "status", None)
                    if status in ("pending", "running"):
                        rid = r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", None)
                        if rid:
                            run_ids.add(rid)
            except Exception:
                pass

        if not run_ids:
            self._active_run_id = None
            return CancellationResult(success=True, cancelled_run_ids=(), failed_run_ids=(), terminal_statuses={})

        cancelled_ids: list[str] = []
        failed_ids: list[str] = []
        terminal_statuses: dict[str, str] = {}

        async def _cancel_one(run_id: str) -> None:
            try:
                await asyncio.wait_for(
                    client.runs.cancel(thread_id, run_id, wait=True, action="interrupt"),
                    timeout=_RUN_CANCEL_WAIT_SECONDS,
                )
            except Exception as exc:
                logger.warning("Cancellation wait failed for run %s on thread %s: %s", run_id, thread_id, exc)

            try:
                info = await client.runs.get(thread_id, run_id)
                st = info.get("status") if isinstance(info, dict) else getattr(info, "status", "")
                st = str(st).lower()
                terminal_statuses[run_id] = st
                if st in ("interrupted", "cancelled", "error", "success"):
                    cancelled_ids.append(run_id)
                else:
                    failed_ids.append(run_id)
            except Exception:
                failed_ids.append(run_id)

        await asyncio.gather(*(_cancel_one(rid) for rid in set(run_ids)))

        # DV-09: Only clear _active_run_id if it was confirmed terminal
        if self._active_run_id:
            if self._active_run_id in cancelled_ids:
                self._active_run_id = None

        all_ok = len(failed_ids) == 0
        return CancellationResult(
            success=all_ok,
            cancelled_run_ids=tuple(cancelled_ids),
            failed_run_ids=tuple(failed_ids),
            terminal_statuses=terminal_statuses,
        )

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> Any:
        """Write a state update into the thread checkpoint.

        Delegates to ``RemoteGraph.aupdate_state`` with retry on 409 conflict.
        """
        graph = self._get_graph()
        prepared = _prepare_config(config)

        # Sanitize values to ensure messages are JSON serializable
        clean_values = dict(values)
        if "messages" in clean_values and isinstance(clean_values["messages"], list):
            clean_msgs = []
            for m in clean_values["messages"]:
                if hasattr(m, "content") and hasattr(m, "type"):
                    msg_dict: dict[str, Any] = {
                        "role": getattr(m, "type", "system"),
                        "content": str(m.content),
                    }
                    add_kwargs = getattr(m, "additional_kwargs", None)
                    if isinstance(add_kwargs, Mapping) and add_kwargs:
                        msg_dict["additional_kwargs"] = dict(add_kwargs)
                    msg_id = getattr(m, "id", None)
                    if msg_id is not None:
                        msg_dict["id"] = msg_id
                    clean_msgs.append(msg_dict)
                elif isinstance(m, dict):
                    clean_msgs.append(m)
                else:
                    clean_msgs.append(str(m))
            clean_values["messages"] = clean_msgs

        kwargs: dict[str, Any] = {}
        if as_node is not None:
            kwargs["as_node"] = as_node

        try:
            return await graph.aupdate_state(prepared, clean_values, **kwargs)
        except Exception as exc:
            # If 409 Conflict: Thread is busy, cancel any dangling active run and retry once
            if "conflict" in str(exc).lower() or getattr(exc, "status_code", None) == 409:
                logger.debug("aupdate_state encountered conflict, cancelling active runs and retrying")
                await self.acancel_active_runs(config)
                return await graph.aupdate_state(prepared, clean_values, **kwargs)

            if as_node is not None:
                return await graph.aupdate_state(prepared, clean_values)
            raise

    async def aput_store_item(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
    ) -> None:
        """Write an item to the server-side LangGraph Store.

        Args:
            namespace: Store namespace.
            key: Item key within `namespace`.
            value: JSON-serializable item value.
        """
        graph = self._get_graph()
        try:
            client = graph._validate_client()
            await client.store.put_item(namespace, key, value, index=False)
        except Exception:
            logger.debug(
                "Failed to write store item %s/%s",
                ".".join(namespace),
                key,
                exc_info=True,
            )
            raise

    async def aensure_thread(self, config: dict[str, Any]) -> None:
        """Ensure the remote thread record exists before reading/mutating state.

        After a server restart, a thread may still have checkpointed state on
        disk while the server's live store has no record of it. This method
        performs idempotent HTTP-side registration so subsequent ``aget_state``
        and ``aupdate_state`` calls work correctly.
        """
        graph = self._get_graph()
        prepared = _prepare_config(config)
        thread_id = prepared["configurable"].get("thread_id")
        if not thread_id:
            return

        metadata = prepared.get("metadata")
        thread_metadata = metadata if isinstance(metadata, dict) else None

        try:
            client = graph._validate_client()
            await client.threads.create(
                thread_id=thread_id,
                if_exists="do_nothing",
                metadata=thread_metadata,
                graph_id=self._graph_name,
            )
        except Exception:
            logger.warning(
                "Failed to ensure thread %s exists on remote server",
                thread_id,
                exc_info=True,
            )
            raise

    async def create_thread(self) -> dict[str, Any]:
        """Create thread using SDK client."""
        client = self._get_graph()._validate_client()
        return await client.threads.create()

    async def afork_thread(self, source_thread_id: str) -> str:
        """Fork a conversation thread using the LangGraph SDK and append a ``<fork_notice>``.

        Workflow:
        1. Copies the source thread's checkpoints and writes via LangGraph SDK
           ``client.threads.copy(source_thread_id)`` (falling back to local SQLite
           ``SessionManager.fork_thread`` if the SDK endpoint fails or is unavailable).
        2. Appends a model-facing ``<fork_notice>`` message (with ``lc_source="fork_notice"``)
           to the newly created branch via ``aupdate_state`` without mutating the source thread.
        """
        from opscloud.middleware.goal_state_notice import build_fork_notice_message
        from opscloud.state.session import SessionManager

        norm_source_tid = normalize_thread_id(source_thread_id)
        new_thread_id: str | None = None

        graph = self._get_graph()
        client: Any = None
        if hasattr(graph, "_validate_client"):
            try:
                client = graph._validate_client()
            except Exception:
                client = None

        if client is not None and hasattr(client, "threads") and hasattr(client.threads, "copy"):
            try:
                try:
                    await self.aensure_thread({"configurable": {"thread_id": norm_source_tid}})
                except Exception:
                    logger.debug("aensure_thread before copy skipped for %s", norm_source_tid, exc_info=True)
                copied = await client.threads.copy(norm_source_tid)
                if isinstance(copied, Mapping) and copied.get("thread_id"):
                    new_thread_id = str(copied["thread_id"])
                elif hasattr(copied, "thread_id") and getattr(copied, "thread_id"):
                    new_thread_id = str(getattr(copied, "thread_id"))
            except Exception:
                logger.debug(
                    "client.threads.copy failed for %s; falling back to SessionManager.fork_thread",
                    norm_source_tid,
                    exc_info=True,
                )

        if not new_thread_id:
            new_thread_id = await SessionManager.fork_thread(norm_source_tid)
            if client is not None and hasattr(client, "threads") and hasattr(client.threads, "create"):
                try:
                    await client.threads.create(
                        thread_id=new_thread_id,
                        if_exists="do_nothing",
                        metadata={"forked_from": source_thread_id},
                        graph_id=self._graph_name,
                    )
                except Exception:
                    logger.debug("client.threads.create fallback skipped for %s", new_thread_id, exc_info=True)

        notice_msg = build_fork_notice_message(
            source_thread_id=source_thread_id,
            new_thread_id=new_thread_id,
        )
        try:
            await self.aupdate_state(
                {"configurable": {"thread_id": new_thread_id}},
                {"messages": [notice_msg]},
            )
        except Exception:
            logger.debug(
                "Could not append <fork_notice> via aupdate_state on forked thread %s",
                new_thread_id,
                exc_info=True,
            )

        return new_thread_id

    async def astream_side_question(
        self,
        question: str,
        *,
        parent_thread_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        messages_override: list[Any] | None = None,
    ) -> AsyncIterator[str]:
        """Stream an isolated `/btw` side question on a forked temporary thread.

        Workflow:
        1. Copies the parent thread's checkpoint into a temporary thread via
           ``client.threads.copy`` (or falls back to creating a temporary thread
           seeded with prior conversation messages).
        2. Streams a single tool-free turn with ``BTW_PROMPT_PREFIX`` and
           ``_btw_side_question=True`` without touching ``self._active_run_id``
           or mutating ``parent_thread_id``.
        3. Always deletes the temporary thread in ``finally:`` via
           ``client.threads.delete(temp_thread_id)``.
        """
        from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
        from opscloud.middleware.btw import format_btw_prompt

        graph = self._get_graph()
        client: Any = None
        if hasattr(graph, "_validate_client"):
            try:
                client = graph._validate_client()
            except Exception:
                client = None

        norm_parent_tid = normalize_thread_id(parent_thread_id) if parent_thread_id else None
        temp_thread_id: str | None = None
        copied_checkpoint = False

        if client is not None and hasattr(client, "threads"):
            if norm_parent_tid and hasattr(client.threads, "copy"):
                try:
                    copied = await client.threads.copy(norm_parent_tid)
                    if isinstance(copied, Mapping) and copied.get("thread_id"):
                        temp_thread_id = str(copied["thread_id"])
                        copied_checkpoint = True
                    elif hasattr(copied, "thread_id") and getattr(copied, "thread_id"):
                        temp_thread_id = str(getattr(copied, "thread_id"))
                        copied_checkpoint = True
                except Exception:
                    logger.debug(
                        "client.threads.copy failed for parent thread %s; falling back to temporary thread creation",
                        norm_parent_tid,
                        exc_info=True,
                    )

            if not temp_thread_id and hasattr(client.threads, "create"):
                try:
                    created = await client.threads.create()
                    if isinstance(created, Mapping) and created.get("thread_id"):
                        temp_thread_id = str(created["thread_id"])
                    elif hasattr(created, "thread_id") and getattr(created, "thread_id"):
                        temp_thread_id = str(getattr(created, "thread_id"))
                except Exception:
                    logger.debug("client.threads.create failed for /btw temp thread", exc_info=True)

        if not temp_thread_id:
            temp_thread_id = normalize_thread_id(None)

        seed_messages: list[Any] = []
        if not copied_checkpoint:
            raw_seed = messages_override
            if not raw_seed and norm_parent_tid:
                try:
                    state = await self.aget_state({"configurable": {"thread_id": norm_parent_tid}})
                    values = getattr(state, "values", None) or (state.get("values") if isinstance(state, Mapping) else None)
                    if isinstance(values, Mapping) and isinstance(values.get("messages"), list):
                        raw_seed = list(values["messages"])
                except Exception:
                    raw_seed = None
            if raw_seed:
                for m in raw_seed:
                    m_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, Mapping) else None)
                    if m_type in ("human", "HumanMessage", "ai", "AIMessage"):
                        seed_messages.append(m)

        side_msg = HumanMessage(
            content=format_btw_prompt(question),
            additional_kwargs={"ephemeral": True},
        )
        run_input: dict[str, Any] = {
            "messages": [*seed_messages, side_msg],
            "_btw_side_question": True,
        }

        payload: dict[str, Any] = dict(context) if isinstance(context, Mapping) else {}
        payload["thread_id"] = temp_thread_id
        payload["_btw_side_question"] = True

        side_run_id: str | None = None
        run_completed = False

        def _on_side_run_created(run_meta: Any) -> None:
            nonlocal side_run_id
            rid = getattr(run_meta, "run_id", None) or (
                run_meta.get("run_id") if isinstance(run_meta, Mapping) else None
            )
            if rid:
                side_run_id = str(rid)

        def _extract_chunk_text(msg_obj: Any) -> str:
            content = getattr(msg_obj, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                return "".join(parts)
            return ""

        yielded_any = False
        side_stream_kwargs: dict[str, Any] = {}
        if hasattr(graph, "_validate_client"):
            side_stream_kwargs["on_run_created"] = _on_side_run_created
        try:
            async for raw_item in graph.astream(
                run_input,
                stream_mode=["messages", "updates"],
                subgraphs=False,
                config={"configurable": {"thread_id": temp_thread_id}},
                context=payload,
                **side_stream_kwargs,
            ):
                parsed = _parse_stream_item(raw_item)
                if parsed is None:
                    continue
                _ns, mode, data = parsed
                if mode == "messages":
                    msg_dict, meta = data if isinstance(data, tuple) else (data, {})
                    if isinstance(meta, Mapping) and meta.get("run_id"):
                        side_run_id = str(meta["run_id"])
                    if isinstance(meta, Mapping) and meta.get("lc_source") == "auto_mode_classifier":
                        continue
                    msg_obj = _convert_message_data(msg_dict) if isinstance(msg_dict, dict) else (
                        msg_dict if isinstance(msg_dict, BaseMessage) else None
                    )
                    if isinstance(msg_obj, (AIMessage, AIMessageChunk)) or getattr(msg_obj, "type", "") in (
                        "ai",
                        "AIMessage",
                        "AIMessageChunk",
                    ):
                        chunk_text = _extract_chunk_text(msg_obj)
                        if chunk_text:
                            yielded_any = True
                            yield chunk_text
                elif mode == "updates" and not yielded_any and isinstance(data, Mapping):
                    for node_val in data.values():
                        if not isinstance(node_val, Mapping):
                            continue
                        msgs = node_val.get("messages")
                        if isinstance(msgs, list):
                            for m in msgs:
                                m_obj = _convert_message_data(m) if isinstance(m, dict) else m
                                if isinstance(m_obj, (AIMessage, AIMessageChunk)) or getattr(
                                    m_obj, "type", ""
                                ) in ("ai", "AIMessage", "AIMessageChunk"):
                                    txt = _extract_chunk_text(m_obj)
                                    if txt:
                                        yielded_any = True
                                        yield txt
            run_completed = True
        finally:
            if client is not None:
                if not run_completed and side_run_id and hasattr(client, "runs") and hasattr(client.runs, "cancel"):
                    try:
                        await client.runs.cancel(temp_thread_id, side_run_id, wait=False, action="interrupt")
                    except Exception:
                        logger.debug("Failed to cancel aborted /btw run %s", side_run_id, exc_info=True)
                if hasattr(client, "threads") and hasattr(client.threads, "delete"):
                    try:
                        await client.threads.delete(temp_thread_id)
                    except Exception:
                        logger.debug("Failed to delete temporary /btw thread %s", temp_thread_id, exc_info=True)


class InProcessAgent(RemoteAgent):
    """In-process LangGraph agent wrapper matching ``RemoteAgent``'s interface.

    Instead of spawning a separate ``langgraph dev`` HTTP server process and
    serializing every streaming token over localhost HTTP/SSE, ``InProcessAgent``
    streams directly from the compiled ``Pregel`` graph in the same Python process
    and event loop, sharing ``MCPSessionManager``, ``OpsCloudCheckpointer``, and
    ``InMemoryStore`` with zero IPC overhead.
    """

    is_in_process: bool = True

    def __init__(
        self,
        graph: Any,
        *,
        checkpointer: Any = None,
        store: Any = None,
        graph_name: str = "agent",
        cwd: str | None = None,
    ) -> None:
        super().__init__(url="inprocess://local", graph_name=graph_name)
        self._cwd = cwd
        self._graph = graph
        self._checkpointer = checkpointer
        self._store = store
        self._active_tasks: set[asyncio.Task[Any]] = set()

    def _get_graph(self) -> Any:
        return self._graph

    async def aclose(self) -> None:
        """Cancel any active in-process stream tasks without tearing down shared graph state."""
        for task in list(self._active_tasks):
            if not task.done():
                task.cancel()
        self._active_tasks.clear()
        self._active_run_id = None

    async def aensure_thread(self, thread_id: str | None = None) -> str:
        """Ensure a valid UUID thread ID (in-process checkpointer creates threads on write)."""
        return normalize_thread_id(thread_id)

    async def create_thread(self) -> str:
        """Generate a fresh UUID thread ID."""
        return normalize_thread_id(None)

    async def astream(
        self,
        input: dict[str, Any] | Any,
        config: Mapping[str, Any] | None = None,
        *,
        stream_mode: list[str] | str | None = None,
        subgraphs: bool = False,
        context: Any | None = None,
        durability: str | None = None,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        """Stream directly from the in-process compiled LangGraph ``Pregel`` instance."""
        from langchain_core.messages import BaseMessage
        from opscloud.agent.config import CLIContextSchema

        prepared_config = _prepare_config(config)
        thread_id = _require_thread_id(prepared_config)
        await self.aensure_thread(thread_id)

        payload = (
            dict(context)
            if isinstance(context, Mapping)
            else (dict(context.__dict__) if hasattr(context, "__dict__") else {})
        )
        if "configurable" in prepared_config:
            conf = dict(prepared_config["configurable"])
            tid = conf.pop("thread_id", None) or thread_id
            if tid and "thread_id" not in payload:
                payload["thread_id"] = tid
            for k, v in conf.items():
                if k not in payload:
                    payload[k] = v
            prepared_config["configurable"] = {"thread_id": tid} if tid else {}

        if self._cwd and "cwd" not in payload:
            payload["cwd"] = self._cwd

        if payload.get("thread_id") and not payload.get("approval_mode_key"):
            from opscloud.security.approval_mode import approval_mode_key

            payload["approval_mode_key"] = approval_mode_key(str(payload["thread_id"]))

        active_tid = payload.get("thread_id") or thread_id
        if active_tid:
            from opscloud.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID

            ACTIVE_SESSION_THREAD_ID.set(str(active_tid))

        stream_context: Any = CLIContextSchema.from_payload(payload) if payload else context

        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_tasks.add(current_task)

        stream_kwargs: dict[str, Any] = {}
        if durability is not None:
            stream_kwargs["durability"] = durability

        dropped_count = 0
        try:
            async for raw_item in self._graph.astream(
                input,
                stream_mode=stream_mode or ["messages", "updates"],
                subgraphs=subgraphs,
                config=prepared_config,
                context=stream_context,
                **stream_kwargs,
            ):
                parsed = _parse_stream_item(raw_item)
                if parsed is None:
                    continue
                ns, mode, data = parsed

                if mode == "messages":
                    msg_dict, meta = data if isinstance(data, tuple) else (data, {})
                    if isinstance(meta, dict) and meta.get("run_id"):
                        self._active_run_id = str(meta["run_id"])
                    if isinstance(msg_dict, BaseMessage):
                        yield (ns, "messages", (msg_dict, meta or {}))
                    elif isinstance(msg_dict, dict):
                        msg_obj = _convert_message_data(msg_dict)
                        if msg_obj is not None:
                            yield (ns, "messages", (msg_obj, meta or {}))
                        else:
                            dropped_count += 1
                    continue

                if mode == "updates" and isinstance(data, dict):
                    update_data = data
                    if "__interrupt__" in data:
                        update_data = {
                            **data,
                            "__interrupt__": _convert_interrupts(data["__interrupt__"]),
                        }
                    yield (ns, "updates", update_data)
                    continue

                yield (ns, mode, data)
        finally:
            if current_task is not None:
                self._active_tasks.discard(current_task)
            self._active_run_id = None
            if dropped_count:
                logger.warning("Dropped %d message(s) during in-process stream", dropped_count)

    async def aget_state(self, config: dict[str, Any]) -> Any:
        _require_thread_id(config)
        try:
            return await self._graph.aget_state(_prepare_config(config))
        except Exception as exc:
            if "subscriptable" in str(exc).lower():
                return None
            logger.debug("InProcessAgent.aget_state failed: %s", exc)
            return None

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> Any:
        prepared = _prepare_config(config)
        kwargs: dict[str, Any] = {}
        if as_node is not None:
            kwargs["as_node"] = as_node
        try:
            return await self._graph.aupdate_state(prepared, values, **kwargs)
        except Exception:
            if as_node is not None:
                return await self._graph.aupdate_state(prepared, values)
            raise

    async def acancel_active_runs(self, config: dict[str, Any]) -> CancellationResult:
        _require_thread_id(config)
        cancelled: list[str] = []
        for task in list(self._active_tasks):
            if not task.done():
                task.cancel()
                cancelled.append(self._active_run_id or "inprocess-run")
        self._active_tasks.clear()
        self._active_run_id = None
        return CancellationResult(
            success=True,
            cancelled_run_ids=tuple(cancelled),
            failed_run_ids=(),
            terminal_statuses=dict.fromkeys(cancelled, "cancelled"),
        )

    async def aput_store_item(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
    ) -> None:
        if self._store is not None and hasattr(self._store, "aput"):
            await self._store.aput(namespace, key, value)
        elif self._store is not None and hasattr(self._store, "put"):
            self._store.put(namespace, key, value)



def _prepare_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    configurable = dict(config.get("configurable", {}))
    config["configurable"] = configurable
    return config


def _convert_interrupts(raw: Any) -> list[Any]:
    from langgraph.types import Interrupt

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    results: list[Any] = []
    for item in raw:
        if isinstance(item, (list, tuple)):
            results.extend(_convert_interrupts(item))
        elif isinstance(item, Interrupt):
            results.append(item)
        elif isinstance(item, dict) and "value" in item:
            results.append(Interrupt(value=item["value"], id=item.get("id", "")))
        else:
            results.append(item)
    return results


def _convert_ai_message(data: dict[str, Any]) -> Any:
    from langchain_core.messages import AIMessageChunk

    content = data.get("content", "")
    tool_call_chunks = data.get("tool_call_chunks", [])
    tool_calls = data.get("tool_calls", [])
    usage_metadata = data.get("usage_metadata")
    response_metadata = data.get("response_metadata", {})

    additional_kwargs = dict(data.get("additional_kwargs") or {})
    if "reasoning_content" in data and "reasoning_content" not in additional_kwargs:
        additional_kwargs["reasoning_content"] = data["reasoning_content"]
    if "thinking" in data and "thinking" not in additional_kwargs:
        additional_kwargs["thinking"] = data["thinking"]

    kwargs: dict[str, Any] = {
        "content": content,
        "id": data.get("id"),
        "response_metadata": response_metadata,
    }
    if additional_kwargs:
        kwargs["additional_kwargs"] = additional_kwargs

    if tool_call_chunks:
        kwargs["tool_call_chunks"] = [
            {
                "name": tc.get("name"),
                "args": tc.get("args", ""),
                "id": tc.get("id"),
                "index": tc.get("index", i),
            }
            for i, tc in enumerate(tool_call_chunks)
        ]
    elif tool_calls:
        has_str_args = any(isinstance(tc.get("args"), str) for tc in tool_calls)
        if has_str_args:
            kwargs["tool_call_chunks"] = [
                {
                    "name": tc.get("name"),
                    "args": tc.get("args", ""),
                    "id": tc.get("id"),
                    "index": i,
                }
                for i, tc in enumerate(tool_calls)
            ]
        else:
            kwargs["tool_calls"] = tool_calls

    try:
        chunk = AIMessageChunk(**kwargs)
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct AIMessageChunk from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None

    if usage_metadata:
        chunk.usage_metadata = usage_metadata
    return chunk


def _convert_human_message(data: dict[str, Any]) -> Any:
    from langchain_core.messages import HumanMessage

    content = data.get("content", "")
    additional_kwargs = dict(data.get("additional_kwargs") or {})
    try:
        kwargs: dict[str, Any] = {"content": content, "id": data.get("id")}
        if additional_kwargs:
            kwargs["additional_kwargs"] = additional_kwargs
        return HumanMessage(**kwargs)
    except Exception:
        return None


def _convert_tool_message(data: dict[str, Any]) -> Any:
    from langchain_core.messages import ToolMessage

    content = data.get("content", "")
    tool_call_id = data.get("tool_call_id", "")
    try:
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=data.get("name", ""),
            id=data.get("id"),
            status=data.get("status", "success"),
        )
    except Exception:
        return None


_MESSAGE_CONVERTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "ai": _convert_ai_message,
    "AIMessage": _convert_ai_message,
    "AIMessageChunk": _convert_ai_message,
    "human": _convert_human_message,
    "HumanMessage": _convert_human_message,
    "tool": _convert_tool_message,
    "ToolMessage": _convert_tool_message,
}


def _convert_message_data(data: dict[str, Any]) -> Any:
    msg_type = data.get("type", "")
    converter = _MESSAGE_CONVERTERS.get(msg_type)
    if converter is not None:
        return converter(data)
    if "content" in data:
        return _convert_ai_message(data)
    return None
