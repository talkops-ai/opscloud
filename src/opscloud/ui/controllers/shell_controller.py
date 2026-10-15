"""Shell command execution controller extracted from `OpsCloudApp`."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from opscloud.ui.widgets.messages import (
    AssistantMessage,
    ErrorMessage,
    SystemMessage,
    UserMessage,
)
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)

_SHELL_TIMEOUT_SECONDS = 60
"""Maximum time a shell command is allowed to run before being killed."""


class ShellController:
    """Manages local `!` shell command execution, cancellation, and context buffering."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    async def handle_shell_command(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        """Handle a shell command (`!` prefix)."""
        app = self._app
        if not incognito:
            await app._mount_message(UserMessage(f"!{command}"))
        app._shell_running = True

        app._shell_worker = app.run_worker(
            app._run_shell_task(command, incognito=incognito),
            exclusive=False,
        )

    async def run_shell_task(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        """Run a shell command in a background worker."""
        app = self._app
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(sys.platform != "win32"),
            )
            app._shell_process = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_SHELL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await app._kill_shell_process()
                await app._mount_message(
                    ErrorMessage(f"Command timed out ({_SHELL_TIMEOUT_SECONDS}s limit)")
                )
                return
            except asyncio.CancelledError:
                await app._kill_shell_process()
                raise

            output = (stdout_bytes or b"").decode(errors="replace").strip()
            stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
            if stderr_text:
                output += f"\n[stderr]\n{stderr_text}"

            if output:
                if incognito:
                    await app._mount_message(
                        SystemMessage(f"```text\n{output}\n```")
                    )
                else:
                    await app._mount_message(
                        AssistantMessage(f"```text\n{output}\n```")
                    )
            else:
                await app._mount_message(
                    SystemMessage("Command completed (no output)")
                )

            if proc.returncode and proc.returncode != 0:
                await app._mount_message(
                    ErrorMessage(f"Exit code: {proc.returncode}")
                )

            if not incognito:
                app._buffer_shell_for_model_context(command, output, proc.returncode)

        except asyncio.CancelledError:
            logger.debug("Shell task cancelled")
        except Exception as e:
            logger.exception("Shell command failed: %s", e)
            await app._mount_message(ErrorMessage(f"Shell error: {e!r}"))
        finally:
            app._shell_running = False
            app._shell_process = None
            app._shell_worker = None
            # Drain queue after shell completes
            await app._process_next_from_queue()

    async def kill_shell_process(self) -> None:
        """Terminate the active shell process."""
        proc = self._app._shell_process
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            with suppress(ProcessLookupError):
                proc.kill()

    def buffer_shell_for_model_context(
        self, command: str, output: str, returncode: int | None
    ) -> None:
        """Buffer a non-incognito `!` command/output for the next user send."""
        from langchain_core.messages import HumanMessage

        code = returncode if returncode is not None else "unknown"
        body = output or "(no output)"
        content = (
            "<user_shell_command>\n"
            "<command>\n"
            f"{command}\n"
            "</command>\n"
            "<result>\n"
            f"Exit code: {code}\n"
            "Output:\n"
            f"{body}\n"
            "</result>\n"
            "</user_shell_command>"
        )
        self._app._pending_shell_messages.append(HumanMessage(content=content))

    async def flush_pending_shell_messages(self) -> None:
        """Write buffered `!` command/output into thread state, then clear it."""
        app = self._app
        if not app._pending_shell_messages:
            return
        thread_id = getattr(app, "_agent_thread_id", None) or getattr(
            getattr(app, "_session_state", None), "thread_id", None
        )
        agent_obj = getattr(app, "_agent", None)
        if not agent_obj or not thread_id:
            return

        messages = app._pending_shell_messages
        app._pending_shell_messages = []
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        try:
            await agent_obj.aupdate_state(config, {"messages": messages})
        except Exception:
            logger.warning(
                "Failed to flush pending shell messages to model context", exc_info=True
            )
