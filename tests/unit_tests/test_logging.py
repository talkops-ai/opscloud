"""Unit tests for OpsCloud logging configuration and subsystems.

Covers:
- Default log paths under /tmp/opscloud_logs
- Configurable log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- File logging enabled by default and console logging disabled by default
- Per-thread log file binding (bind_logging_to_thread)
- JSON structured formatting vs human-readable plain text formatting
- CLI flags --log-level, --log-dir, --log-file and main() integration
- Doctor diagnostics integration for logging
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from opscloud.cli.main import build_parser, main
from opscloud.utils.logger import (
    DEFAULT_LOG_DIR,
    DEFAULT_LOG_FILE,
    AgentLogger,
    bind_logging_to_thread,
    configure_logging,
    get_active_log_dir,
    get_active_log_file,
    get_active_thread_log_file,
    get_configured_log_level,
    get_logger,
    is_console_logging_enabled,
    resolve_log_dir,
    resolve_log_file,
    resolve_log_level,
    resolve_log_mode,
    resolve_log_to_console,
    resolve_log_to_file,
)


def test_default_log_paths():
    """Default log directory and file point to /tmp/opscloud_logs."""
    assert resolve_log_dir() == DEFAULT_LOG_DIR
    assert resolve_log_file() == DEFAULT_LOG_DIR / DEFAULT_LOG_FILE


def test_resolve_log_paths_custom_and_env(monkeypatch, tmp_path):
    """Custom path arguments and env vars override defaults."""
    custom_dir = tmp_path / "custom_logs"
    assert resolve_log_dir(custom_dir) == custom_dir
    assert custom_dir.exists()

    monkeypatch.setenv("OPSCLOUD_LOG_DIR", str(tmp_path / "env_logs"))
    resolved_env = resolve_log_dir()
    assert resolved_env == tmp_path / "env_logs"
    assert resolved_env.exists()

    # Custom log file (relative vs absolute)
    assert resolve_log_file("agent.log", log_dir=custom_dir) == custom_dir / "agent.log"
    abs_file = tmp_path / "specific" / "run.log"
    assert resolve_log_file(abs_file) == abs_file


def test_resolve_log_level():
    """Log level resolution handles strings, ints, aliases, and env vars."""
    assert resolve_log_level("debug") == logging.DEBUG
    assert resolve_log_level("INFO") == logging.INFO
    assert resolve_log_level("warn") == logging.WARNING
    assert resolve_log_level("warning") == logging.WARNING
    assert resolve_log_level("error") == logging.ERROR
    assert resolve_log_level("CRITICAL") == logging.CRITICAL
    assert resolve_log_level(logging.DEBUG) == logging.DEBUG

    with patch.dict("os.environ", {"OPSCLOUD_LOG_LEVEL": "ERROR"}):
        assert resolve_log_level() == logging.ERROR


def test_console_and_file_defaults(monkeypatch):
    """Console logging defaults to False (silent) and file logging defaults to True."""
    monkeypatch.delenv("OPSCLOUD_LOG_CONSOLE", raising=False)
    monkeypatch.delenv("LOG_TO_CONSOLE", raising=False)
    monkeypatch.delenv("OPSCLOUD_LOG_TO_FILE", raising=False)
    monkeypatch.delenv("LOG_TO_FILE", raising=False)

    assert resolve_log_to_console() is False
    assert resolve_log_to_file() is True

    # Truthy env vars enable console
    monkeypatch.setenv("OPSCLOUD_LOG_CONSOLE", "true")
    assert resolve_log_to_console() is True


def test_resolve_log_mode(monkeypatch):
    """Log mode defaults to text and switches to json when requested."""
    assert resolve_log_mode() == "text"
    assert resolve_log_mode("json") == "json"

    monkeypatch.setenv("OPSCLOUD_LOG_MODE", "json")
    assert resolve_log_mode() == "json"


def test_file_logging_execution(tmp_path):
    """Configuring logging to a directory creates files and captures messages."""
    log_dir = tmp_path / "test_run_logs"
    log_file = log_dir / "test.log"

    configure_logging(
        level="INFO",
        log_to_console=False,
        log_to_file=True,
        log_dir=log_dir,
        log_file=log_file,
    )

    test_logger = get_logger("unit_test")
    test_logger.info("Test informative message", extra={"task": "cloud-audit"})
    test_logger.warning("Test warning message")

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "Test informative message" in content
    assert "Test warning message" in content
    assert "unit_test" in content
    assert "task=cloud-audit" in content


def test_per_thread_log_binding(tmp_path):
    """bind_logging_to_thread sets thread context and routes all logs to single unified log file."""
    log_dir = tmp_path / "thread_logs"
    configure_logging(
        level="DEBUG",
        log_to_console=False,
        log_to_file=True,
        log_dir=log_dir,
        log_file="main.log",
    )

    thread_id = "01a0f1d5-d696-72a3-8af6-5022cff9d454"
    unified_log_path = bind_logging_to_thread(thread_id)
    assert unified_log_path == log_dir / "main.log"
    assert get_active_thread_log_file() == unified_log_path

    logger = get_logger("thread_worker")
    logger.info("Executing thread-scoped step 1")
    logger.debug("Debug information for thread")

    # Verify no separate {thread_id}.log file was created
    assert not (log_dir / f"{thread_id}.log").exists()

    # Verify unified log file received the log record
    main_content = unified_log_path.read_text(encoding="utf-8")
    assert "Executing thread-scoped step 1" in main_content
    assert "Debug information for thread" in main_content


def test_json_mode_logging(tmp_path):
    """JSON mode produces valid single-line JSON records."""
    log_dir = tmp_path / "json_logs"
    log_file = log_dir / "structured.jsonl"

    configure_logging(
        level="INFO",
        log_to_console=False,
        log_to_file=True,
        log_dir=log_dir,
        log_file=log_file,
        mode="json",
    )

    logger = get_logger("json_worker")
    logger.info("Operation succeeded", extra={"duration_ms": 42, "region": "eu-west-1"})

    assert log_file.exists()
    lines = [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 1
    last_record = json.loads(lines[-1])
    assert last_record["level"] == "INFO"
    assert last_record["agent"] == "json_worker"
    assert last_record["message"] == "Operation succeeded"
    assert last_record["extra"]["duration_ms"] == 42
    assert last_record["extra"]["region"] == "eu-west-1"


def test_cli_flags_parsed():
    """Parser recognizes --log-level, --log-dir, and --log-file."""
    parser = build_parser()
    args = parser.parse_args([
        "-p", "Hello",
        "--log-level", "debug",
        "--log-dir", "/tmp/custom_dir",
        "--log-file", "custom.log",
        "--verbose",
    ])
    assert args.log_level == "debug"
    assert args.log_dir == "/tmp/custom_dir"
    assert args.log_file == "custom.log"
    assert args.verbose is True


def test_main_logging_configuration(monkeypatch, tmp_path):
    """main() entrypoint configures logging and silences console in quiet/json modes."""
    log_dir = tmp_path / "main_logs"
    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("opscloud.cli.main.run_non_interactive", mock_run)

    # 1. Normal run with custom log-dir
    ret = main(["Audit infra", "--log-dir", str(log_dir), "--log-level", "warning"])
    assert ret == 0
    assert get_active_log_dir() == log_dir
    assert get_configured_log_level() == "WARNING"
    assert is_console_logging_enabled() is False

    # 2. Verbose run enables console
    ret = main(["Audit infra", "--verbose"])
    assert ret == 0
    assert get_configured_log_level() == "DEBUG"
    assert is_console_logging_enabled() is True

    # 3. JSON run keeps console silent even if not verbose
    ret = main(["Audit infra", "--json"])
    assert ret == 0
    assert is_console_logging_enabled() is False


def test_doctor_command_reports_logging(capsys, monkeypatch):
    """Doctor command outputs active log destination and level."""
    from opscloud.cli.commands.doctor import run_doctor_command

    fake_ctx = MagicMock(is_authenticated=True, account_id="123456789012", region="us-east-1", profile="default", error=None)
    monkeypatch.setattr("opscloud.cli.commands.doctor.get_aws_context", lambda: fake_ctx)

    # 1. Table format
    ret = run_doctor_command("text")
    assert ret == 0
    captured = capsys.readouterr()
    assert "Log Destination" in captured.out

    # 2. JSON format
    ret = run_doctor_command("json")
    assert ret == 0
    captured_json = capsys.readouterr()
    payload = json.loads(captured_json.out.strip())
    assert "logging" in payload["data"]
    assert "directory" in payload["data"]["logging"]
    assert "file" in payload["data"]["logging"]
    assert "level" in payload["data"]["logging"]


def test_rotating_file_handler_applied(tmp_path):
    """File handlers use RotatingFileHandler with size limit and backup count."""
    from logging.handlers import RotatingFileHandler
    from opscloud.utils.logger import _build_file_handler

    test_file = tmp_path / "rotating_test.log"
    handler = _build_file_handler(test_file, logging.DEBUG, is_json=False)
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 3
    handler.close()


@pytest.mark.asyncio
async def test_headless_run_emits_lifecycle_logs(tmp_path, monkeypatch):
    """Headless run logs execution start, tool calls, subagents, and completion to log file."""
    from opscloud.cli.non_interactive import _run_headless
    from opscloud.utils.logger import configure_logging, get_active_log_file

    log_dir = tmp_path / "headless_logs"
    configure_logging(log_dir=log_dir, level="INFO", log_to_file=True, log_to_console=False)

    class MockRemoteAgent:
        async def astream(self, *args, **kwargs):
            yield ("root", "messages", "Hello from agent")
            mock_tool = MagicMock()
            mock_tool.name = "aws:describe_instances"
            yield ("root", "updates", {"messages": [mock_tool]})
            yield ("root", "custom", {"type": "subagent", "phase": "complete", "subagent_type": "security-auditor", "duration_ms": 120})

    from contextlib import contextmanager

    @contextmanager
    def mock_server_session(cfg):
        yield MockRemoteAgent()

    monkeypatch.setattr("opscloud.cli.non_interactive.server_session", mock_server_session)

    ret = await _run_headless(
        prompt="Inspect AWS security",
        quiet=True,
        json_output=True,
    )
    assert ret == 0

    log_file = get_active_log_file()
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")

    assert "Starting headless run" in content
    assert "Tool executed: aws:describe_instances" in content
    assert "Subagent security-auditor (complete)" in content
    assert "Headless run completed in" in content


