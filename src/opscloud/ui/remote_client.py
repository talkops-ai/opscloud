"""Remote agent client — wrapper around LangGraph's RemoteGraph.

Delegates streaming, state management, and SSE handling to
langgraph.pregel.remote.RemoteGraph. Converts streamed message dicts into
LangChain message objects for TextualAdapter.
"""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
from collections.abc import AsyncIterator, Mapping
from typing import Any, Callable

logger = get_logger(__name__)

_RUN_CANCEL_WAIT_SECONDS = 10.0


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

    def _get_graph(self) -> Any:
        if self._graph is None:
            from langgraph.pregel.remote import RemoteGraph

            self._graph = RemoteGraph(
                self._graph_name,
                url=self._url,
                api_key=self._api_key,
                headers=self._headers if self._headers else None,
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

        graph = self._get_graph()
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

        async for raw_item in graph.astream(
            input,
            stream_mode=stream_mode or ["messages", "updates"],
            subgraphs=subgraphs,
            config=prepared_config,
            context=stream_context,
        ):
            parsed = _parse_stream_item(raw_item)
            if parsed is None:
                continue
            ns, mode, data = parsed

            logger.debug("RemoteGraph event ns=%s mode=%s data_type=%s", ns, mode, type(data).__name__)

            if mode == "messages":
                msg_dict, meta = data if isinstance(data, tuple) else (data, {})
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

    async def acancel_active_runs(self, config: dict[str, Any]) -> None:
        """Cancel any in-flight runs on the thread so the server is not left busy."""
        thread_id = _require_thread_id(config)
        graph = self._get_graph()
        try:
            client = graph._validate_client()
            runs = await client.runs.list(thread_id)
            active_runs = [
                r for r in runs if isinstance(r, dict) and r.get("status") in ("pending", "running")
            ]
            if not active_runs:
                return

            async def _cancel_one(run_id: str) -> None:
                try:
                    await asyncio.wait_for(
                        client.runs.cancel(thread_id, run_id, wait=True, action="interrupt"),
                        timeout=_RUN_CANCEL_WAIT_SECONDS,
                    )
                except Exception:
                    pass

            await asyncio.gather(*(_cancel_one(r["run_id"]) for r in active_runs if "run_id" in r))
        except Exception:
            logger.debug("Failed to cancel active runs for thread %s", thread_id, exc_info=True)

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
                    clean_msgs.append({"role": getattr(m, "type", "system"), "content": str(m.content)})
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


def _prepare_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    configurable = dict(config.get("configurable", {}))
    config["configurable"] = configurable
    return config


def _convert_interrupts(raw: Any) -> list[Any]:
    from langgraph.types import Interrupt

    if not isinstance(raw, list):
        return [raw] if raw is not None else []
    results = []
    for item in raw:
        if isinstance(item, Interrupt):
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
    try:
        return HumanMessage(content=content, id=data.get("id"))
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
