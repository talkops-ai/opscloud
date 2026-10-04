"""Unix-domain-socket event bus for opscloud.

Allows external CLI tools, background runners, watchdogs, or CI/CD pipelines
to push commands, prompts, and signals into a running OpsCloud session over
a local Unix socket using newline-delimited JSON.

Wire protocol
~~~~~~~~~~~~~

**Request** (one JSON object per line)::

    {"kind": "prompt", "payload": "Review AWS security group rules", "source": "ci-pipeline"}
    {"kind": "signal", "payload": "interrupt", "source": "watchdog"}
    {"kind": "command", "payload": "/compact", "source": "automation"}

**Response** (JSON line echoed back with optional correlation_id)::

    {"ok": true, "correlation_id": "req-123"}
    {"ok": false, "error": "invalid kind: foobar", "correlation_id": "req-123"}
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
from enum import IntEnum
import json
from opscloud.utils.logger import get_logger
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Literal, cast, get_args

logger = get_logger(__name__)

# ── Types ────────────────────────────────────────────────

ExternalEventKind = Literal["command", "prompt", "signal"]
"""Top-level event kinds carried over the wire protocol."""

ExternalSignal = Literal["interrupt", "force-clear"]
"""Allowed signal payloads accepted by the event listener."""

EventSink = Callable[["ExternalEvent"], Awaitable[None]]
"""Type alias for async callback receiving parsed events."""

_VALID_KINDS: frozenset[str] = frozenset(get_args(ExternalEventKind))
_VALID_SIGNALS: frozenset[str] = frozenset(get_args(ExternalSignal))

_MAX_LINE_BYTES: int = 64 * 1024  # 64 KiB per JSON line limit
_CLIENT_IDLE_TIMEOUT_SECONDS: float = 60.0


class BypassTier(IntEnum):
    """Priority tier for incoming events."""

    QUEUED = 0
    """Normal priority — enters the async event queue."""

    IMMEDIATE = 1
    """Urgent priority — bypasses regular queues (e.g. interrupt signal)."""


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """A validated event received over the Unix socket."""

    kind: ExternalEventKind
    """``"command"``, ``"prompt"``, or ``"signal"``."""

    payload: str
    """The event content (prompt text, command string, or signal name)."""

    source: str
    """Identifier of the sending process / system."""

    bypass: BypassTier = BypassTier.QUEUED
    """Delivery priority."""

    correlation_id: str | None = None
    """Optional caller-provided correlation ID for request tracking."""

    def __post_init__(self) -> None:
        """Validate invariant constraints.

        Raises:
            ValueError: If kind is unknown, payload is empty, or signal is unrecognized.
        """
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"Unknown external event kind: {self.kind!r}; expected one of {sorted(_VALID_KINDS)}")
        if not self.payload or not self.payload.strip():
            raise ValueError("External event payload must be a non-empty string")
        if self.kind == "signal" and self.payload.strip().lower() not in _VALID_SIGNALS:
            raise ValueError(
                f"Unknown external signal: {self.payload!r}; expected one of {sorted(_VALID_SIGNALS)}"
            )


# ── Socket Path Helper ────────────────────────────────────


def default_unix_socket_path() -> Path:
    """Return a safe, per-process Unix socket path for OpsCloud.

    Uses ``OPSCLOUD_EVENT_SOCKET`` if set, then prefers ``XDG_RUNTIME_DIR``,
    and falls back to system temporary directory with short names to avoid
    macOS AF_UNIX 104-byte limit.

    Returns:
        Path to use for the Unix domain socket.
    """
    env_override = os.environ.get("OPSCLOUD_EVENT_SOCKET")
    if env_override:
        return Path(env_override)

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidate = Path(runtime_dir) / "opscloud" / f"events-{os.getpid():d}.sock"
        if len(str(candidate)) < 100:
            return candidate

    tmp = Path(tempfile.gettempdir())
    return tmp / f"opscloud-{os.getpid():d}.sock"


def _unlink_existing_socket(path: Path) -> None:
    """Remove a stale Unix socket without touching regular files or directories.

    Args:
        path: Path of the candidate socket to remove.

    Raises:
        FileNotFoundError: If path does not exist.
        FileExistsError: If path exists but is not a socket.
        OSError: If unlinking fails.
    """
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode):
        raise FileExistsError(f"Refusing to unlink non-socket file at {path}")
    path.unlink()


# ── EventBus ─────────────────────────────────────────────


class EventBus:
    """Async Unix-domain-socket server that receives :class:`ExternalEvent` objects.

    Supports two consumption patterns:
    1. Queue-based: Consumers call ``await bus.get_event()``.
    2. Callback-based: Started with an ``EventSink`` callback.

    Usage::

        bus = EventBus()
        await bus.start("/tmp/opscloud.sock")
        try:
            while True:
                event = await bus.get_event()
                print(event)
        finally:
            await bus.stop()
    """

    def __init__(self, queue_size: int = 256) -> None:
        """Initialize EventBus.

        Args:
            queue_size: Maximum items retained in the internal queue.
        """
        self._queue: asyncio.Queue[ExternalEvent] = asyncio.Queue(maxsize=queue_size)
        self._server: asyncio.AbstractServer | None = None
        self._socket_path: Path | None = None
        self._sink: EventSink | None = None

    # ── Lifecycle ────────────────────────────────────────

    async def start(
        self,
        socket_path: str | Path | None = None,
        sink: EventSink | None = None,
    ) -> Path:
        """Bind the Unix socket and start accepting connections.

        Args:
            socket_path: Path for the Unix domain socket. Defaults to
                ``default_unix_socket_path()``.
            sink: Optional async callback invoked for each received event.
                When None, events are enqueued for ``get_event()``.

        Returns:
            The bound socket path.

        Raises:
            RuntimeError: If the server is already running.
            FileExistsError: If socket_path exists and is not a socket.
            OSError: If socket creation or permission setting fails.
        """
        if self._server is not None:
            raise RuntimeError("EventBus server is already running")

        path = Path(socket_path) if socket_path else default_unix_socket_path()
        self._sink = sink

        # Clean up stale socket if present
        if path.exists():
            with contextlib.suppress(FileNotFoundError):
                _unlink_existing_socket(path)

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        prev_umask = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(path),
                limit=_MAX_LINE_BYTES,
            )
        finally:
            os.umask(prev_umask)

        self._socket_path = path

        # Ensure 0o600 permissions
        with contextlib.suppress(OSError):
            path.chmod(0o600)

        logger.info("OpsCloud event bus listening on %s", path)
        return path

    async def stop(self) -> None:
        """Shut down the server and remove the socket file."""
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        if self._socket_path is not None:
            path = self._socket_path
            self._socket_path = None
            if path.exists():
                try:
                    _unlink_existing_socket(path)
                except (FileNotFoundError, OSError) as exc:
                    logger.debug("Could not remove socket %s: %s", path, exc)

    @property
    def running(self) -> bool:
        """Whether the server is actively listening."""
        return self._server is not None and self._server.is_serving()

    @property
    def socket_path(self) -> Path | None:
        """The currently bound socket path, or None if stopped."""
        return self._socket_path

    # ── Consumer API ─────────────────────────────────────

    async def get_event(self) -> ExternalEvent:
        """Block until an event is available and return it.

        Returns:
            The next ExternalEvent from the queue.
        """
        return await self._queue.get()

    def get_event_nowait(self) -> ExternalEvent | None:
        """Return the next event or None if the queue is empty.

        Returns:
            The next ExternalEvent or None.
        """
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ── Connection handler ───────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Process one client connection reading newline-delimited JSON envelopes.

        Args:
            reader: Stream reader for client socket.
            writer: Stream writer for client socket.
        """
        peer = writer.get_extra_info("peername") or "<local>"
        logger.debug("Event bus client connected: %s", peer)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(),
                        timeout=_CLIENT_IDLE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.debug("Closing idle event bus client %s", peer)
                    break
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    logger.warning("Event bus line exceeded read limit: %s", exc)
                    await self._send_response(writer, ok=False, error="line exceeds read limit")
                    break

                if not line:
                    break

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                correlation_id: str | None = None
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        cand = data.get("correlation_id")
                        if isinstance(cand, str):
                            correlation_id = cand
                except json.JSONDecodeError as exc:
                    await self._send_response(
                        writer,
                        ok=False,
                        error=f"invalid JSON: {exc}",
                        correlation_id=correlation_id,
                    )
                    continue

                event, error = self._validate_event(data)
                if error is not None:
                    await self._send_response(
                        writer,
                        ok=False,
                        error=error,
                        correlation_id=correlation_id,
                    )
                    continue

                assert event is not None
                correlation_id = event.correlation_id

                if self._sink is not None:
                    try:
                        await self._sink(event)
                    except Exception as exc:
                        logger.exception("EventBus sink callback raised an error: %s", exc)
                        await self._send_response(
                            writer,
                            ok=False,
                            error=f"sink failed: {exc}",
                            correlation_id=correlation_id,
                        )
                        continue
                else:
                    try:
                        self._queue.put_nowait(event)
                    except asyncio.QueueFull:
                        await self._send_response(
                            writer,
                            ok=False,
                            error="event queue full",
                            correlation_id=correlation_id,
                        )
                        continue

                await self._send_response(writer, ok=True, correlation_id=correlation_id)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.debug("Event bus client disconnected: %s", peer)

    # ── Validation ───────────────────────────────────────

    @staticmethod
    def _validate_event(
        data: Any,
    ) -> tuple[ExternalEvent | None, str | None]:
        """Parse and validate a raw JSON payload into an ExternalEvent.

        Args:
            data: Decoded JSON data object.

        Returns:
            Tuple of (ExternalEvent, None) on success, or (None, error_str) on failure.
        """
        if not isinstance(data, dict):
            return None, "expected JSON object"

        kind = data.get("kind")
        if kind not in _VALID_KINDS:
            return None, f"invalid kind: {kind!r} (expected one of {sorted(_VALID_KINDS)})"

        validated_kind = cast(ExternalEventKind, kind)

        payload = data.get("payload", "")
        if not isinstance(payload, str) or not payload.strip():
            return None, "payload must be a non-empty string"

        source = data.get("source", "unknown")
        if not isinstance(source, str):
            source = "unknown"

        if validated_kind == "signal" and payload.strip().lower() not in _VALID_SIGNALS:
            return None, f"invalid signal: {payload!r} (expected one of {sorted(_VALID_SIGNALS)})"

        raw_bypass = data.get("bypass")
        if raw_bypass is not None:
            try:
                bypass = BypassTier(raw_bypass)
            except ValueError:
                bypass = BypassTier.IMMEDIATE if validated_kind == "signal" else BypassTier.QUEUED
        else:
            bypass = BypassTier.IMMEDIATE if validated_kind == "signal" else BypassTier.QUEUED

        correlation_id = data.get("correlation_id")
        if correlation_id is not None and not isinstance(correlation_id, str):
            correlation_id = None

        try:
            event = ExternalEvent(
                kind=validated_kind,
                payload=payload.strip(),
                source=source,
                bypass=bypass,
                correlation_id=correlation_id,
            )
            return event, None
        except ValueError as exc:
            return None, str(exc)

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    async def _send_response(
        writer: asyncio.StreamWriter,
        *,
        ok: bool,
        error: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Send a JSON response line back to the connected client.

        Args:
            writer: Stream writer for response.
            ok: Whether operation succeeded.
            error: Error message when ok is False.
            correlation_id: Optional caller tracking identifier.
        """
        resp: dict[str, Any] = {"ok": ok}
        if error is not None:
            resp["error"] = error
        if correlation_id is not None:
            resp["correlation_id"] = correlation_id

        try:
            writer.write(json.dumps(resp).encode("utf-8") + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass


__all__ = [
    "BypassTier",
    "EventBus",
    "EventSink",
    "ExternalEvent",
    "ExternalEventKind",
    "ExternalSignal",
    "default_unix_socket_path",
]
