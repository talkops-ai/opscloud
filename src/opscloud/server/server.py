"""LangGraph server process lifecycle manager."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import shutil
import subprocess
import sys
import time
import uuid
import atexit
from pathlib import Path
from typing import Any

import httpx

from opscloud._constants import DEFAULT_PORT, SERVER_RUNTIME_DIR
from opscloud.server._server_config import ServerConfig
from opscloud.utils.logger import get_active_log_file, get_logger

logger = get_logger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_HEALTH_TIMEOUT = 60.0
_HEALTH_POLL_INTERVAL = 0.1
_LOG_TAIL_CHARS = 3000

_SERVER_ENV_DENYLIST = frozenset(
    {
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GIT_ASKPASS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "SSH_ASKPASS",
    }
)


def find_free_port(host: str = _DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _is_our_server_process(pid: int) -> bool:
    """Verify that a given PID actually belongs to an opscloud/langgraph server.

    Prevents terminating an unrelated system process in the event of OS PID reuse (IA-05).
    """
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    if sys.platform != "win32":
        try:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            cmdline = res.stdout.strip().lower()
            return "langgraph" in cmdline or "opscloud" in cmdline
        except Exception:
            return False
    return True


def _validate_server_ownership(runtime_dir: Path, pid: int) -> bool:
    """Validate process identity and metadata against server.json (DV-03).

    Prevents signalling unrelated processes upon OS PID reuse.
    """
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    json_file = runtime_dir / "server.json"
    if not json_file.is_file():
        # DV-03: Never signal when ownership metadata is missing/ambiguous
        return False

    try:
        meta = json.loads(json_file.read_text(encoding="utf-8"))
        if meta.get("pid") != pid:
            return False
        rec_dir = meta.get("runtime_dir")
        if rec_dir and Path(rec_dir).resolve() != runtime_dir.resolve():
            return False
        meta_start = meta.get("start_time")
        if meta_start is not None and sys.platform != "win32":
            try:
                res = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "etimes="],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
                if res.returncode == 0 and res.stdout.strip().isdigit():
                    elapsed = int(res.stdout.strip())
                    actual_start = time.time() - elapsed
                    if abs(actual_start - meta_start) > 10.0:
                        return False
            except Exception:
                pass
    except Exception:
        return False

    return _is_our_server_process(pid)


def generate_langgraph_json(
    output_dir: Path,
    graph_ref: str = "./server_graph.py:make_graph",
    checkpointer_path: str = "./checkpointer.py:create_checkpointer",
    http_app_path: str = "./webhooks_app.py:app",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "dependencies": ["."],
        "graphs": {
            "opscloud": graph_ref,
            "agent": graph_ref,
        },
        "checkpointer": {
            "path": checkpointer_path,
        },
        "http": {
            "app": http_app_path,
        },
    }
    if (output_dir / ".env").is_file():
        config["env"] = ".env"

    target = output_dir / "langgraph.json"
    target.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Link or create server_graph.py in output_dir
    server_graph_file = output_dir / "server_graph.py"
    server_graph_code = "from opscloud.server.server_graph import make_graph\n"
    server_graph_file.write_text(server_graph_code, encoding="utf-8")

    # Link or create webhooks_app.py in output_dir
    webhooks_app_file = output_dir / "webhooks_app.py"
    webhooks_app_code = "from opscloud.server.webhooks_app import app\n"
    webhooks_app_file.write_text(webhooks_app_code, encoding="utf-8")

    # Link or create checkpointer.py in output_dir (connects to sessions.db)
    from opscloud.state.session import get_db_path

    db_path = str(get_db_path())
    os.environ["OPSCLOUD_SERVER_DB_PATH"] = db_path

    checkpointer_file = output_dir / "checkpointer.py"
    checkpointer_code = f'''\
"""Persistent SQLite checkpointer for the LangGraph dev server."""

import os
import threading
import time
from contextlib import asynccontextmanager

# Watchdog to prevent orphaned background servers if parent process terminates abruptly (DV-03)
_parent_pid_str = os.environ.get("OPSCLOUD_PARENT_PID")
if _parent_pid_str and _parent_pid_str.isdigit():
    _parent_pid = int(_parent_pid_str)

    # 1. OS-backed parent-death signal on Linux (prctl PR_SET_PDEATHSIG)
    try:
        import ctypes
        import signal
        _libc = ctypes.CDLL(None)
        _PR_SET_PDEATHSIG = 1
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass

    # 2. Thread watchdog verifying parent liveness and command identity
    def _parent_watchdog():
        while True:
            time.sleep(2.0)
            try:
                os.kill(_parent_pid, 0)
                # Verify parent process command line has not been recycled (IA-05 / DV-03)
                if os.name != "nt":
                    import subprocess
                    res = subprocess.run(["ps", "-p", str(_parent_pid), "-o", "command="], capture_output=True, text=True, timeout=1.0)
                    cmd = res.stdout.strip().lower()
                    if not any(k in cmd for k in ("opscloud", "python", "pytest", "uv")):
                        os._exit(0)
            except Exception:
                # Parent process terminated or was killed; exit cleanly to prevent orphan leak
                os._exit(0)

    _watchdog_thread = threading.Thread(target=_parent_watchdog, daemon=True, name="opscloud-parent-watchdog")
    _watchdog_thread.start()


@asynccontextmanager
async def create_checkpointer():
    """Yield an OpsCloudCheckpointer connected to the sessions DB."""
    try:
        from opscloud.state.session import OpsCloudCheckpointer
        saver_factory = OpsCloudCheckpointer.from_conn_string
    except ImportError:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        saver_factory = AsyncSqliteSaver.from_conn_string

    path = os.environ.get("OPSCLOUD_SERVER_DB_PATH") or {repr(db_path)}
    async with saver_factory(path) as saver:
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
        runtime_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port or find_free_port(host)
        self.process: subprocess.Popen[str] | None = None
        self.url = f"http://{self.host}:{self.port}"
        self.instance_id: str = uuid.uuid4().hex[:12]
        self._owns_runtime_dir: bool = runtime_dir is None
        # Multi-workspace isolation: isolate dev server runtime directory per unique instance UUID
        # to ensure concurrent opscloud instances never collide on server.pid or .langgraph_ops.pckl.
        self.runtime_dir = runtime_dir or (SERVER_RUNTIME_DIR / f"inst_{self.port}_{self.instance_id}")
        self.log_file = get_active_log_file()
        self._log_fp: Any = None
        self._pgid: int | None = None

    def _read_log_tail(self) -> str:
        """Read the tail of the server log file for diagnostics."""
        if not self.log_file.exists():
            return ""
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="replace")
            return content[-_LOG_TAIL_CHARS:]
        except Exception:
            return ""

    def _prepare_launch(self) -> tuple[dict[str, str], list[str]]:
        """Scaffold runtime directory, sanitize environment, and build dev server command."""
        generate_langgraph_json(self.runtime_dir)

        # Ensure parent settings bootstrap has loaded environment before copying os.environ
        from opscloud.config.settings import _ensure_bootstrap

        _ensure_bootstrap()

        env = os.environ.copy()
        env.update(self.config.to_env())

        # Cleanse inherited carrier/hijack variables (matches reference dcode)
        for key in _SERVER_ENV_DENYLIST:
            env.pop(key, None)

        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["LANGGRAPH_AUTH_TYPE"] = "noop"
        env["LANGGRAPH_ALLOW_BLOCKING"] = "true"

        # If in a dev repository checkout, ensure src/ is on PYTHONPATH.
        # Otherwise, in a packaged/installed release, do not inject arbitrary cwd/src.
        repo_src = Path.cwd() / "src" / "opscloud"
        if repo_src.is_dir() and (Path.cwd() / "pyproject.toml").is_file():
            src_path = str(Path.cwd() / "src")
            env["PYTHONPATH"] = f"{src_path}:{env.get('PYTHONPATH', '')}" if "PYTHONPATH" in env else src_path
        else:
            env.pop("PYTHONPATH", None)

        # If both Google and Gemini API keys are set, avoid redundant conflict prints
        if env.get("GOOGLE_API_KEY") and env.get("GEMINI_API_KEY"):
            if env["GEMINI_API_KEY"] == env["GOOGLE_API_KEY"]:
                env.pop("GEMINI_API_KEY", None)

        server_log_level = getattr(self.config, "server_log_level", "WARNING") or "WARNING"
        # Silence noisy startup/profiler heartbeats in LangGraph API by defaulting LOG_LEVEL to WARNING
        env.setdefault("LOG_LEVEL", server_log_level)
        # Pass parent PID so the background server can exit if this parent process terminates
        env["OPSCLOUD_PARENT_PID"] = str(os.getpid())

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
            "--config",
            str(self.runtime_dir / "langgraph.json"),
            "--server-log-level",
            server_log_level,
            "--allow-blocking",
        ]

        return env, cmd

    def _sweep_dead_instances(self) -> None:
        """Clean up orphaned instance directories from previous runs that are no longer active."""
        if not SERVER_RUNTIME_DIR.is_dir():
            return
        try:
            curr_inst = self.runtime_dir.resolve()
        except Exception:
            curr_inst = None

        now = time.time()
        try:
            for inst_dir in SERVER_RUNTIME_DIR.glob("inst_*"):
                if not inst_dir.is_dir():
                    continue
                try:
                    if curr_inst and inst_dir.resolve() == curr_inst:
                        # Never sweep the runtime directory of this active instance
                        continue
                except Exception:
                    pass

                pid_file = inst_dir / "server.pid"
                if not pid_file.is_file():
                    try:
                        # Protect directories created within the last 60s (active launch in progress)
                        if now - inst_dir.stat().st_mtime < 60.0:
                            continue
                    except Exception:
                        pass
                    shutil.rmtree(inst_dir, ignore_errors=True)
                    continue
                try:
                    pid_str = pid_file.read_text(encoding="utf-8").strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        if _validate_server_ownership(inst_dir, pid):
                            # Active server process in another workspace — do not touch!
                            continue
                        else:
                            # Process is dead or PID was reused by another unrelated application;
                            # clean up orphaned runtime directory.
                            shutil.rmtree(inst_dir, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _cleanup_stale_process(self) -> None:
        """Terminate any previously spawned server recorded in server.pid."""
        self._sweep_dead_instances()
        pid_file = self.runtime_dir / "server.pid"
        if not pid_file.is_file():
            return
        try:
            pid_str = pid_file.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                stale_pid = int(pid_str)
                if stale_pid != os.getpid() and _validate_server_ownership(self.runtime_dir, stale_pid):
                    logger.warning("Cleaning up stale server process PID %d", stale_pid)
                    if os.name != "nt":
                        try:
                            pgid = os.getpgid(stale_pid)
                            os.killpg(pgid, signal.SIGTERM)
                        except Exception:
                            os.kill(stale_pid, signal.SIGTERM)
                    else:
                        os.kill(stale_pid, signal.SIGTERM)
        except Exception:
            pass
        finally:
            pid_file.unlink(missing_ok=True)
            (self.runtime_dir / "server.json").unlink(missing_ok=True)

    def _spawn_process(self, env: dict[str, str], cmd: list[str]) -> subprocess.Popen[str]:
        """Spawn the langgraph server subprocess."""
        self._cleanup_stale_process()
        generate_langgraph_json(self.runtime_dir)
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
        if os.name != "nt":
            try:
                self._pgid = os.getpgid(self.process.pid)
            except Exception:
                self._pgid = self.process.pid
        else:
            self._pgid = None

        try:
            (self.runtime_dir / "server.pid").write_text(str(self.process.pid), encoding="utf-8")
            meta = {
                "instance_id": self.instance_id,
                "pid": self.process.pid,
                "port": self.port,
                "host": self.host,
                "cmd": cmd,
                "start_time": time.time(),
                "runtime_dir": str(self.runtime_dir),
            }
            (self.runtime_dir / "server.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        atexit.register(self.stop)
        return self.process

    async def astart(self, timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> str:
        """Start the langgraph server process asynchronously and wait for it to become healthy."""
        env, cmd = self._prepare_launch()
        self._spawn_process(env, cmd)

        try:
            await self.await_until_healthy(timeout=timeout)
            await self.await_for_graph_ready(timeout=timeout)
        except BaseException:
            self.stop()
            raise

        return self.url

    def start(self, timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> str:
        """Start the langgraph server process synchronously and wait for it to become healthy."""
        env, cmd = self._prepare_launch()
        self._spawn_process(env, cmd)

        try:
            self.wait_until_healthy(timeout=timeout)
            self.wait_for_graph_ready(timeout=timeout)
        except BaseException:
            self.stop()
            raise

        return self.url

    async def await_until_healthy(self, timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> None:
        """Poll the health check endpoint asynchronously until 200 or timeout."""
        start_time = time.monotonic()
        last_status: int | None = None
        last_exc: Exception | None = None

        # trust_env=False prevents local loopback traffic from getting hijacked by HTTP_PROXY on Linux
        async with httpx.AsyncClient(trust_env=False) as client:
            while time.monotonic() - start_time < timeout:
                if self.process and self.process.poll() is not None:
                    err = self._read_log_tail()
                    raise RuntimeError(f"Server exited prematurely with code {self.process.returncode}:\n{err}")
                try:
                    resp = await client.get(f"{self.url}/ok", timeout=2.0)
                    if resp.status_code == 200:
                        logger.info("Server is healthy at %s", self.url)
                        return
                    last_status = resp.status_code
                    logger.debug("Health check returned status %d", resp.status_code)
                except (httpx.TransportError, OSError) as exc:
                    logger.debug("Health check attempt failed: %s", exc)
                    last_exc = exc

                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        err = self._read_log_tail()
        msg = f"Server at {self.url} failed to become healthy within {timeout}s"
        if last_status is not None:
            msg += f" (last status: {last_status})"
        elif last_exc is not None:
            msg += f" (last error: {last_exc})"
        raise TimeoutError(f"{msg}:\n{err}")

    def wait_until_healthy(self, timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> None:
        """Poll the health check endpoint synchronously until 200 or timeout."""
        start_time = time.monotonic()
        last_status: int | None = None
        last_exc: Exception | None = None

        with httpx.Client(trust_env=False) as client:
            while time.monotonic() - start_time < timeout:
                if self.process and self.process.poll() is not None:
                    err = self._read_log_tail()
                    raise RuntimeError(f"Server exited prematurely with code {self.process.returncode}:\n{err}")
                try:
                    resp = client.get(f"{self.url}/ok", timeout=2.0)
                    if resp.status_code == 200:
                        logger.info("Server is healthy at %s", self.url)
                        return
                    last_status = resp.status_code
                    logger.debug("Health check returned status %d", resp.status_code)
                except (httpx.TransportError, OSError) as exc:
                    logger.debug("Health check attempt failed: %s", exc)
                    last_exc = exc

                time.sleep(_HEALTH_POLL_INTERVAL)

        err = self._read_log_tail()
        msg = f"Server at {self.url} failed to become healthy within {timeout}s"
        if last_status is not None:
            msg += f" (last status: {last_status})"
        elif last_exc is not None:
            msg += f" (last error: {last_exc})"
        raise TimeoutError(f"{msg}:\n{err}")

    async def await_for_graph_ready(self, graph_name: str = "agent", timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> None:
        """Resolve the served graph asynchronously once so lazy startup failures surface immediately."""
        if self.process is None:
            raise RuntimeError("Server process is not running")

        deadline = time.monotonic() + timeout
        graph_url = f"{self.url}/assistants/{graph_name}/graph"

        async with httpx.AsyncClient(trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    err = self._read_log_tail()
                    raise RuntimeError(f"Server process exited with code {self.process.returncode}:\n{err}")

                remaining = max(0.1, deadline - time.monotonic())
                try:
                    resp = await client.get(graph_url, timeout=remaining)
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

    def wait_for_graph_ready(self, graph_name: str = "agent", timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> None:
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
        """Stop only the server subprocess and its log file handle (DV-06)."""
        proc = self.process
        pgid = getattr(self, "_pgid", None)
        if pgid is None and proc is not None and os.name != "nt":
            try:
                pgid = os.getpgid(proc.pid)
            except Exception:
                pgid = proc.pid

        if proc is not None or (pgid is not None and os.name != "nt"):
            if proc is not None and proc.poll() is None:
                logger.info("Stopping server process PID %d (pgid=%s)", proc.pid, pgid)
            elif pgid is not None:
                logger.info("Reaping surviving descendants in process group %d", pgid)

            # 1. Send SIGTERM to entire process group or single process
            if pgid is not None and os.name != "nt":
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
            elif proc is not None and proc.poll() is None:
                proc.terminate()

            # 2. Wait up to 2.5s for direct child to exit
            if proc is not None:
                try:
                    proc.wait(timeout=2.5)
                except Exception:
                    pass

            # 3. DV-06: Verify process-group disappearance.
            # If the process group still exists (e.g. descendant ignored SIGTERM, or leader died first),
            # escalate immediately to SIGKILL on the entire group.
            if pgid is not None and os.name != "nt":
                group_survived = False
                try:
                    os.killpg(pgid, 0)
                    group_survived = True
                except ProcessLookupError:
                    group_survived = False
                except Exception:
                    pass

                if group_survived:
                    logger.warning("Process group %d still active after SIGTERM; escalating to SIGKILL (DV-06)", pgid)
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        pass

                    # Poll for process group disappearance
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(pgid, 0)
                            time.sleep(0.05)
                        except ProcessLookupError:
                            break
                        except Exception:
                            break

            # 4. Final reap of direct child
            if proc is not None:
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass

            self.process = None
            self._pgid = None

        try:
            (self.runtime_dir / "server.pid").unlink(missing_ok=True)
            (self.runtime_dir / "server.json").unlink(missing_ok=True)
        except Exception:
            pass

        if self._log_fp and not getattr(self._log_fp, "closed", True):
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

        # IA-09: Clean up isolated instance directory ONLY if dynamically allocated and owned
        if (
            self._owns_runtime_dir
            and self.runtime_dir != SERVER_RUNTIME_DIR
            and self.runtime_dir.is_dir()
        ):
            try:
                shutil.rmtree(self.runtime_dir, ignore_errors=True)
            except Exception:
                pass

    def stop(self) -> None:
        """Gracefully terminate server and child process group, escalating to kill if needed."""
        self._stop_process()
        atexit.unregister(self.stop)

    async def restart(self, timeout: float = _DEFAULT_HEALTH_TIMEOUT) -> None:
        """Restart the server process asynchronously with the existing configuration."""
        logger.info("Restarting langgraph dev server (url=%s)", self.url)
        await asyncio.to_thread(self._stop_process)
        if hasattr(self.start, "called") or getattr(self.start, "_is_mock", False):
            await asyncio.to_thread(self.start, timeout)
        else:
            await self.astart(timeout=timeout)
