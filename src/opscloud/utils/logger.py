"""Logging module for OpsCloud Agent.

Provides color-coded, multi-sink (file + optional console) logging with
both structured (JSON) and human-readable output modes.

By default, all internal logging is automatically written to `/tmp/opscloud_logs/opscloud.log`
(and per-thread log files `/tmp/opscloud_logs/{thread_id}.log`) without polluting
standard console output. Console logging is optional and activated via `--verbose`
or `OPSCLOUD_LOG_CONSOLE=true`.

Usage::

    from opscloud.utils.logger import get_logger, configure_logging, bind_logging_to_thread

    logger = get_logger(__name__)
    logger.info("Module created", extra={"region": "us-east-1"})
    logger.warning("Drift detected", task_id="aws-plan-42")
    logger.error("Validation failed", extra={"exit_code": 1})
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
from enum import Enum
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any

from colorama import Fore, Style

DEFAULT_LOG_DIR = Path("/tmp/opscloud_logs")
DEFAULT_LOG_FILE = "opscloud.log"

LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _LevelColor(Enum):
    """Traffic-light colors for log levels."""

    DEBUG = Fore.LIGHTBLACK_EX
    INFO = Fore.BLUE
    WARNING = Fore.YELLOW
    ERROR = Fore.RED
    CRITICAL = Fore.LIGHTRED_EX


_config_cache: Any | None = ...


def _get_config() -> Any:
    """Lazy-load settings once. Returns the Settings instance or None."""
    global _config_cache
    if _config_cache is ...:
        try:
            from opscloud.config.settings import get_settings

            _config_cache = get_settings()
        except Exception:
            _config_cache = None
    return _config_cache


def _cfg(key: str, fallback: Any) -> Any:
    """Read a single config value, falling back if Settings isn't available."""
    cfg = _get_config()
    if cfg is None:
        return fallback
    return getattr(cfg, key.lower(), getattr(cfg, key, fallback))


def _classify_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return None


class _ColorFormatter(logging.Formatter):
    """Applies per-level color to log records for console output."""

    def __init__(self) -> None:
        super().__init__()
        self._date_fmt: str = _cfg("LOG_DATE_FORMAT", "%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        agent = getattr(record, "agent_name", "BASE")

        try:
            lc = _LevelColor[level].value
        except KeyError:
            lc = Fore.WHITE

        ts = datetime.now(UTC).strftime(self._date_fmt)
        msg = record.getMessage()

        extras = getattr(record, "_structured_extra", None)
        if extras:
            extra_pieces = [f"{k}={v}" for k, v in extras.items()]
            extra_str = " | " + " ".join(extra_pieces)
        else:
            extra_str = ""

        formatted = f"{lc}[{level}]{Style.RESET_ALL} {agent}: [{ts}] {msg}{extra_str}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                formatted = formatted.rstrip() + "\n" + record.exc_text
        return formatted


class _PlainFormatter(logging.Formatter):
    """Emits human-readable text without ANSI color codes (for file logging)."""

    def __init__(self) -> None:
        super().__init__()
        self._date_fmt: str = _cfg("LOG_DATE_FORMAT", "%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        agent = getattr(record, "agent_name", "BASE")
        ts = datetime.now(UTC).strftime(self._date_fmt)
        msg = record.getMessage()

        extras = getattr(record, "_structured_extra", None)
        if extras:
            extra_pieces = [f"{k}={v}" for k, v in extras.items()]
            extra_str = " | " + " ".join(extra_pieces)
        else:
            extra_str = ""

        formatted = f"[{level}] {agent}: [{ts}] {msg}{extra_str}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                formatted = formatted.rstrip() + "\n" + record.exc_text
        return formatted


class _JsonFormatter(logging.Formatter):
    """Emits each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "agent": getattr(record, "agent_name", "BASE"),
            "logger": record.name,
            "message": record.getMessage(),
        }

        for attr in ("task_id", "session_id", "turn_id", "model"):
            val = getattr(record, attr, None)
            if val is not None:
                entry[attr] = val

        extras = getattr(record, "_structured_extra", None)
        if extras:
            entry["extra"] = extras

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            entry["exception"] = record.exc_text

        return json.dumps(entry, default=str)


# ── Global Logging State ─────────────────────────────────

_active_loggers: dict[str, AgentLogger] = {}
_active_loggers_lock = threading.RLock()

_current_log_dir: Path = DEFAULT_LOG_DIR
_current_log_file: Path = DEFAULT_LOG_DIR / DEFAULT_LOG_FILE
_current_level: int = logging.INFO
_current_log_to_console: bool = False
_current_log_to_file: bool = True
_current_log_mode: str = "text"
_active_thread_id: str | None = None

_main_file_handler: logging.Handler | None = None
_console_handler: logging.StreamHandler | None = None


def resolve_log_dir(candidate: str | Path | None = None) -> Path:
    """Resolve the directory where log files should be written."""
    if candidate is not None:
        p = Path(candidate)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    env_dir = os.environ.get("OPSCLOUD_LOG_DIR") or os.environ.get("OPSCLOUD_LOG_DIRECTORY")
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    cfg_dir = _cfg("LOG_DIR", None)
    if cfg_dir:
        p = Path(cfg_dir)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return DEFAULT_LOG_DIR


def resolve_log_file(candidate: str | Path | None = None, log_dir: Path | None = None) -> Path:
    """Resolve the main log file path."""
    target_dir = log_dir or resolve_log_dir()

    if candidate is not None:
        p = Path(candidate)
        return p if p.is_absolute() else (target_dir / p)

    env_file = os.environ.get("OPSCLOUD_LOG_FILE")
    if env_file:
        p = Path(env_file)
        return p if p.is_absolute() else (target_dir / p)

    cfg_file = _cfg("LOG_FILE", None)
    if cfg_file:
        p = Path(cfg_file)
        return p if p.is_absolute() else (target_dir / p)

    return target_dir / DEFAULT_LOG_FILE


def resolve_log_level(candidate: str | int | None = None) -> int:
    """Resolve standard logging level integer from candidate, env var, or settings."""
    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str) and candidate.strip():
        up = candidate.strip().upper()
        if up in LOG_LEVELS:
            return LOG_LEVELS[up]

    raw_env = os.environ.get("OPSCLOUD_LOG_LEVEL") or os.environ.get("LOG_LEVEL")
    if raw_env and raw_env.strip():
        up = raw_env.strip().upper()
        if up in LOG_LEVELS:
            return LOG_LEVELS[up]

    cfg_level = _cfg("LOG_LEVEL", "INFO")
    if isinstance(cfg_level, str) and cfg_level.strip().upper() in LOG_LEVELS:
        return LOG_LEVELS[cfg_level.strip().upper()]

    return logging.INFO


def resolve_log_to_console(candidate: bool | None = None) -> bool:
    """Determine whether log messages should be mirrored to console stderr."""
    if candidate is not None:
        return candidate

    env_val = _classify_bool(os.environ.get("OPSCLOUD_LOG_CONSOLE") or os.environ.get("LOG_TO_CONSOLE"))
    if env_val is not None:
        return env_val

    cfg_val = _classify_bool(_cfg("LOG_TO_CONSOLE", False))
    if cfg_val is not None:
        return cfg_val

    return False


def resolve_log_to_file(candidate: bool | None = None) -> bool:
    """Determine whether file logging is enabled."""
    if candidate is not None:
        return candidate

    env_val = _classify_bool(os.environ.get("OPSCLOUD_LOG_TO_FILE") or os.environ.get("LOG_TO_FILE"))
    if env_val is not None:
        return env_val

    cfg_val = _classify_bool(_cfg("LOG_TO_FILE", True))
    if cfg_val is not None:
        return cfg_val

    return True


def resolve_log_mode(candidate: str | None = None) -> str:
    """Determine whether logs should be plain text or structured JSON."""
    if candidate is not None:
        return "json" if candidate.lower() == "json" else "text"

    env_val = os.environ.get("OPSCLOUD_LOG_MODE") or os.environ.get("LOG_MODE")
    if env_val:
        return "json" if env_val.lower() == "json" else "text"

    if _cfg("LOG_STRUCTURED_JSON", False):
        return "json"

    cfg_mode = _cfg("LOG_MODE", "text")
    return "json" if str(cfg_mode).lower() == "json" else "text"


def _build_file_handler(file_path: Path, level: int, is_json: bool) -> logging.Handler:
    """Create and configure a RotatingFileHandler for a given path."""
    file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(
        str(file_path),
        mode="a",
        maxBytes=10 * 1024 * 1024,  # 10 MB cap per log file
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(_JsonFormatter() if is_json else _PlainFormatter())
    return handler


def _build_console_handler(level: int, is_json: bool) -> logging.StreamHandler:
    """Create and configure a StreamHandler for stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_JsonFormatter() if is_json else _ColorFormatter())
    return handler


def _apply_handlers(py_logger: logging.Logger) -> None:
    """Synchronize a python Logger instance with the global active handlers."""
    py_logger.handlers.clear()
    py_logger.propagate = False
    py_logger.setLevel(_current_level)

    if _current_log_to_file and _main_file_handler is not None:
        py_logger.addHandler(_main_file_handler)

    if _current_log_to_console and _console_handler is not None:
        py_logger.addHandler(_console_handler)


def configure_logging(
    *,
    level: str | int | None = None,
    log_to_console: bool | None = None,
    log_to_file: bool | None = None,
    log_dir: str | Path | None = None,
    log_file: str | Path | None = None,
    mode: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Configure system-wide logging with file and optional console destinations.

    Args:
        level: Log level (e.g. 'DEBUG', 'INFO', 'WARNING', logging.DEBUG).
        log_to_console: True to write logs to sys.stderr (default: False).
        log_to_file: True to write logs to file (default: True).
        log_dir: Directory for log files (default: /tmp/opscloud_logs).
        log_file: File name or path for main log (default: opscloud.log).
        mode: Log format mode ('text' or 'json').
        thread_id: Optional thread ID to bind per-thread logging.
    """
    global _current_log_dir, _current_log_file, _current_level
    global _current_log_to_console, _current_log_to_file, _current_log_mode
    global _main_file_handler, _console_handler, _active_thread_id

    with _active_loggers_lock:
        _current_log_dir = resolve_log_dir(log_dir)
        _current_log_file = resolve_log_file(log_file, _current_log_dir)
        _current_level = resolve_log_level(level)
        _current_log_to_console = resolve_log_to_console(log_to_console)
        _current_log_to_file = resolve_log_to_file(log_to_file)
        _current_log_mode = resolve_log_mode(mode)
        is_json = _current_log_mode == "json"

        # Propagate to environment so child subprocesses inherit active logging configuration
        os.environ["OPSCLOUD_LOG_DIR"] = str(_current_log_dir)
        os.environ["OPSCLOUD_LOG_FILE"] = str(_current_log_file)
        os.environ["OPSCLOUD_LOG_LEVEL"] = get_configured_log_level()
        os.environ["OPSCLOUD_LOG_MODE"] = _current_log_mode
        os.environ["OPSCLOUD_LOG_TO_FILE"] = "true" if _current_log_to_file else "false"

        # Recreate main file handler if file logging enabled
        if _main_file_handler is not None:
            with contextlib.suppress(Exception):
                _main_file_handler.close()
            _main_file_handler = None

        if _current_log_to_file:
            try:
                _main_file_handler = _build_file_handler(_current_log_file, _current_level, is_json)
            except Exception as exc:
                print(f"[OpsCloud Warning] Failed to initialize log file {_current_log_file}: {exc}", file=sys.stderr)

        # Recreate console handler
        if _console_handler is not None:
            _console_handler = None

        if _current_log_to_console:
            _console_handler = _build_console_handler(_current_level, is_json)

        # Handle thread binding if requested
        if thread_id:
            _active_thread_id = thread_id
            os.environ["OPSCLOUD_THREAD_ID"] = thread_id

        # Re-apply updated handlers to all active registered loggers
        for item in _active_loggers.values():
            _apply_handlers(item._logger)


def bind_logging_to_thread(thread_id: str) -> Path:
    """Bind the active thread ID for contextual logging to the unified log file."""
    global _active_thread_id
    with _active_loggers_lock:
        _active_thread_id = thread_id
        os.environ["OPSCLOUD_THREAD_ID"] = thread_id
        return _current_log_file


def get_active_log_file() -> Path:
    """Return the active main log file path."""
    return _current_log_file


def get_active_log_dir() -> Path:
    """Return the active log directory path."""
    return _current_log_dir


def get_active_thread_log_file() -> Path | None:
    """Return the unified log file path."""
    return _current_log_file


def get_configured_log_level() -> str:
    """Return the string representation of current logging level."""
    return logging.getLevelName(_current_level)


def is_console_logging_enabled() -> bool:
    """Check if console logging is currently active."""
    return _current_log_to_console


class AgentLogger:
    """Per-agent logger with structured JSON support, file sinks, and optional console output."""

    __slots__ = ("_logger", "_stream_fn", "agent_name")

    def __init__(self, agent_name: str = "BASE") -> None:
        self.agent_name = agent_name
        self._logger = logging.getLogger(f"opscloud.{agent_name}")
        self._stream_fn: Callable[[dict[str, Any]], None] | None = None
        _apply_handlers(self._logger)

    def set_stream_sink(self, sink_fn: Callable[[dict[str, Any]], None] | None) -> None:
        """Register a real-time event sink function."""
        self._stream_fn = sink_fn

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        extra: dict[str, Any] | None = None,
        exc_info: Any = None,
        **kwargs: Any,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return

        combined_extras: dict[str, Any] = {}
        if extra:
            combined_extras.update(extra)
        if kwargs:
            combined_extras.update(kwargs)

        record_extra: dict[str, Any] = {
            "agent_name": self.agent_name,
            "_structured_extra": combined_extras if combined_extras else None,
        }

        for key in ("task_id", "session_id", "turn_id", "model"):
            if key in combined_extras:
                record_extra[key] = combined_extras[key]

        self._logger._log(level, msg, args, exc_info=exc_info, extra=record_extra)

        if self._stream_fn is not None:
            try:
                self._stream_fn(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "level": logging.getLevelName(level),
                        "agent": self.agent_name,
                        "message": msg % args if args else msg,
                        "extra": combined_extras,
                    }
                )
            except Exception:
                pass

    def debug(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, extra=extra, **kwargs)

    def info(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, extra=extra, **kwargs)

    def warning(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, extra=extra, **kwargs)

    def error(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, exc_info: Any = None, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, extra=extra, exc_info=exc_info, **kwargs)

    def critical(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, exc_info: Any = None, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, *args, extra=extra, exc_info=exc_info, **kwargs)

    def exception(self, msg: str, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, extra=extra, exc_info=True, **kwargs)


def get_logger(name: str = "opscloud") -> AgentLogger:
    """Return an AgentLogger for the specified module or component name."""
    short_name = name
    if short_name.startswith("opscloud."):
        short_name = short_name.replace("opscloud.", "", 1)

    with _active_loggers_lock:
        if short_name not in _active_loggers:
            _active_loggers[short_name] = AgentLogger(short_name)
        return _active_loggers[short_name]


# Initialize default logging on module import
configure_logging()


__all__ = [
    "AgentLogger",
    "DEFAULT_LOG_DIR",
    "DEFAULT_LOG_FILE",
    "LOG_LEVELS",
    "bind_logging_to_thread",
    "configure_logging",
    "get_active_log_dir",
    "get_active_log_file",
    "get_active_thread_log_file",
    "get_configured_log_level",
    "get_logger",
    "is_console_logging_enabled",
    "resolve_log_dir",
    "resolve_log_file",
    "resolve_log_level",
]
