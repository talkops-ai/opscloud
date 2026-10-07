"""LangGraph server process lifecycle manager."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import atexit
from pathlib import Path
from typing import Any

import httpx

from opscloud._constants import DEFAULT_PORT, SERVER_RUNTIME_DIR
from opscloud.server._server_config import ServerConfig
from opscloud.utils.logger import get_active_log_file, get_logger

logger = get_logger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_LOG_TAIL_CHARS = 3000


def find_free_port(host: str = _DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def generate_langgraph_json(
    output_dir: Path,
    graph_ref: str = "./server_graph.py:make_graph",
    checkpointer_path: str = "./checkpointer.py:create_checkpointer",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "dependencies": ["."],
        "graphs": {
            "opscloud": graph_ref,
            "agent": graph_ref,
        },
        "checkpointer": {
            "path": checkpointer_path,
        },
        "env": ".env",
    }
    target = output_dir / "langgraph.json"
    target.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Link or create server_graph.py in output_dir
    server_graph_file = output_dir / "server_graph.py"
    server_graph_code = "from opscloud.server.server_graph import make_graph\n"
    server_graph_file.write_text(server_graph_code, encoding="utf-8")

    # Link or create checkpointer.py in output_dir (connects to sessions.db)
    from opscloud.state.session import get_db_path

    db_path = str(get_db_path())
    os.environ["OPSCLOUD_SERVER_DB_PATH"] = db_path

    checkpointer_file = output_dir / "checkpointer.py"
    checkpointer_code = f'''\
"""Persistent SQLite checkpointer for the LangGraph dev server."""

import os
from contextlib import asynccontextmanager


@asynccontextmanager
async def create_checkpointer():
    """Yield an AsyncSqliteSaver connected to the sessions DB."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = os.environ.get("OPSCLOUD_SERVER_DB_PATH") or {repr(db_path)}
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield saver
'''
    checkpointer_file.write_text(checkpointer_code, encoding="utf-8")
    return target


class ServerProcess:
    """Manages the background langgraph dev subprocess."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        host: str = _DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port or find_free_port(host)
        self.process: subprocess.Popen[str] | None = None
        self.url = f"http://{self.host}:{self.port}"
        self.runtime_dir = SERVER_RUNTIME_DIR
        self.log_file = get_active_log_file()
        self._log_fp: Any = None

    def _read_log_tail(self) -> str:
        """Read the tail of the server log file for diagnostics."""
        if not self.log_file.exists():
            return ""
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="replace")
            return content[-_LOG_TAIL_CHARS:]
        except Exception:
            return ""

    def start(self) -> str:
        """Start the langgraph server process synchronously and wait for it to become healthy."""
        generate_langgraph_json(self.runtime_dir)

        # Ensure parent settings bootstrap has loaded environment before copying os.environ
        from opscloud.config.settings import _ensure_bootstrap

        _ensure_bootstrap()

        env = os.environ.copy()
        env.update(self.config.to_env())

        # If in a dev repository checkout, ensure src/ is on PYTHONPATH.
        # Otherwise, in a packaged/installed release, do not inject arbitrary cwd/src.
        repo_src = Path.cwd() / "src" / "opscloud"
        if repo_src.is_dir() and (Path.cwd() / "pyproject.toml").is_file():
            src_path = str(Path.cwd() / "src")
            env["PYTHONPATH"] = f"{src_path}:{env.get('PYTHONPATH', '')}" if "PYTHONPATH" in env else src_path
        else:
            env.pop("PYTHONPATH", None)

        server_log_level = getattr(self.config, "server_log_level", "WARNING") or "WARNING"
        # Silence noisy startup/profiler heartbeats in LangGraph API by defaulting LOG_LEVEL to WARNING
        env.setdefault("LOG_LEVEL", server_log_level)
        # Prevent blockbuster from raising BlockingError during model/SDK execution in ASGI server
        env["LANGGRAPH_ALLOW_BLOCKING"] = "true"

        cmd = [
            sys.executable,
            "-m",
            "langgraph_cli",
            "dev",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--no-browser",
            "--no-reload",
            "--allow-blocking",
            "--config",
            str(self.runtime_dir / "langgraph.json"),
            "--server-log-level",
            server_log_level,
        ]

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_fp = open(self.log_file, "a", encoding="utf-8")
        logger.info("Spawning server: %s (url=%s, server_log=%s)", " ".join(cmd), self.url, self.log_file)

        popen_kwargs: dict[str, Any] = {
            "cwd": str(self.runtime_dir),
            "env": env,
            "stdout": self._log_fp,
            "stderr": subprocess.STDOUT,
            "text": True,
        }
        # Start in dedicated process group so descendants can be cleanly reaped
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP

        self.process = subprocess.Popen(cmd, **popen_kwargs)
        atexit.register(self.stop)

        try:
            self.wait_until_healthy(timeout=30.0)
            self.wait_for_graph_ready(timeout=30.0)
        except Exception:
            self.stop()
            raise

        return self.url

    def wait_until_healthy(self, timeout: float = 30.0) -> None:
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if self.process and self.process.poll() is not None:
                err = self._read_log_tail()
                raise RuntimeError(f"Server exited prematurely with code {self.process.returncode}:\n{err}")
            try:
                resp = httpx.get(f"{self.url}/ok", timeout=1.0)
                if resp.status_code == 200:
                    logger.info("Server is healthy at %s", self.url)
                    return
            except Exception:
                time.sleep(0.2)
        err = self._read_log_tail()
        raise TimeoutError(f"Server at {self.url} failed to become healthy within {timeout}s:\n{err}")

    def wait_for_graph_ready(self, graph_name: str = "agent", timeout: float = 30.0) -> None:
        """Resolve the served graph once so lazy startup failures surface immediately."""
        if self.process is None:
            raise RuntimeError("Server process is not running")

        deadline = time.monotonic() + timeout
        graph_url = f"{self.url}/assistants/{graph_name}/graph"

        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                err = self._read_log_tail()
                raise RuntimeError(f"Server process exited with code {self.process.returncode}:\n{err}")

            remaining = max(0.1, deadline - time.monotonic())
            try:
                resp = httpx.get(graph_url, timeout=remaining)
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                err = self._read_log_tail()
                if self.process.poll() is not None:
                    msg = f"Server process exited with code {self.process.returncode}:\n{err}"
                else:
                    msg = f"Server graph '{graph_name}' did not initialize within {timeout}s:\n{err}"
                raise RuntimeError(msg) from exc

            if resp.status_code == 200:
                logger.info("Server graph %r is ready at %s", graph_name, self.url)
                return
            elif resp.status_code == 404 and graph_name == "agent":
                graph_name = "opscloud"
                graph_url = f"{self.url}/assistants/{graph_name}/graph"
                continue

            err = self._read_log_tail()
            msg = f"Server graph '{graph_name}' failed readiness check (status: {resp.status_code}):\n{err}"
            raise RuntimeError(msg)

        err = self._read_log_tail()
        raise RuntimeError(f"Server graph '{graph_name}' did not initialize within {timeout}s:\n{err}")

    def _stop_process(self) -> None:
        """Stop only the server subprocess and its log file handle."""
        if self.process and self.process.poll() is None:
            logger.info("Stopping server process PID %d", self.process.pid)
            pgid = None
            if os.name != "nt":
                try:
                    pgid = os.getpgid(self.process.pid)
                except Exception:
                    pass

            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    self.process.terminate()
                self.process.wait(timeout=3.0)
            except Exception:
                if pgid is not None and os.name != "nt":
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        pass
                else:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            self.process = None

        if self._log_fp and not getattr(self._log_fp, "closed", True):
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def stop(self) -> None:
        """Gracefully terminate server and child process group, escalating to kill if needed."""
        self._stop_process()
        atexit.unregister(self.stop)

    async def restart(self, timeout: float = 30.0) -> None:
        """Restart the server process with the existing configuration."""
        logger.info("Restarting langgraph dev server (url=%s)", self.url)
        await asyncio.to_thread(self._stop_process)
        await asyncio.to_thread(self.start)
