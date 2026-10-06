"""``/loop`` — Periodic instruction execution.

Runs a prompt or instruction repeatedly at a specified interval.
Each iteration runs independently with no shared state.
Session-scoped — loops stop when the session ends.
"""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)

_INTERVAL_PATTERN = re.compile(r"^(\d+)(s|m|h|d)$", re.IGNORECASE)
_DEFAULT_INTERVAL_SECONDS = 600  # 10 minutes
_MAX_ITERATIONS = 100


@dataclass
class LoopInstance:
    """A single running loop."""

    id: str
    interval_seconds: int
    instruction: str
    max_iterations: int
    created_at: float = field(default_factory=time.time)
    iteration_count: int = 0
    task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def interval_human(self) -> str:
        s = self.interval_seconds
        if s >= 86400 and s % 86400 == 0:
            return f"{s // 86400}d"
        if s >= 3600 and s % 3600 == 0:
            return f"{s // 3600}h"
        if s >= 60 and s % 60 == 0:
            return f"{s // 60}m"
        return f"{s}s"


class LoopManager:
    """Manages periodic instruction execution.

    Session-scoped singleton attached to the app instance.
    """

    def __init__(self) -> None:
        self._loops: dict[str, LoopInstance] = {}

    async def start(
        self,
        interval: str | None,
        instruction: str,
        *,
        app: Any = None,
        max_iterations: int = _MAX_ITERATIONS,
    ) -> LoopInstance:
        """Start a new loop.

        Args:
            interval: Interval string (e.g. ``"30s"``, ``"5m"``).
            instruction: The prompt/instruction to run each iteration.
            app: App reference for sending messages.
            max_iterations: Maximum number of iterations.

        Returns:
            The created ``LoopInstance``.
        """
        seconds = _parse_interval(interval) if interval else _DEFAULT_INTERVAL_SECONDS
        loop_id = str(uuid.uuid4())[:8]

        instance = LoopInstance(
            id=loop_id,
            interval_seconds=seconds,
            instruction=instruction,
            max_iterations=max_iterations,
        )

        task = asyncio.create_task(
            self._run_loop(instance, app=app),
            name=f"loop-{loop_id}",
        )
        instance.task = task
        self._loops[loop_id] = instance

        return instance

    async def stop(self, loop_id: str | None = None) -> int:
        """Stop a loop by ID, or all loops if ``None``.

        Returns the number of loops stopped.
        """
        if loop_id is not None:
            instance = self._loops.pop(loop_id, None)
            if instance and instance.task and not instance.task.done():
                instance.task.cancel()
                return 1
            return 0

        # Stop all
        count = 0
        for lid in list(self._loops):
            instance = self._loops.pop(lid)
            if instance.task and not instance.task.done():
                instance.task.cancel()
                count += 1
        return count

    @property
    def active(self) -> list[LoopInstance]:
        return [l for l in self._loops.values() if l.is_running]

    @property
    def all(self) -> list[LoopInstance]:
        return list(self._loops.values())

    async def _run_loop(self, instance: LoopInstance, *, app: Any = None) -> None:
        """Run a loop until max iterations or cancellation."""
        try:
            while instance.iteration_count < instance.max_iterations:
                await asyncio.sleep(instance.interval_seconds)
                instance.iteration_count += 1

                logger.info(
                    "Loop %s iteration %d/%d: %s",
                    instance.id,
                    instance.iteration_count,
                    instance.max_iterations,
                    instance.instruction[:50],
                )

                # Send the instruction to the agent
                if app is not None and hasattr(app, "send_agent_message"):
                    try:
                        loop_prompt = (
                            f"[Loop #{instance.id} — iteration {instance.iteration_count}/"
                            f"{instance.max_iterations}]\n\n{instance.instruction}"
                        )
                        await app.send_agent_message(loop_prompt)
                    except Exception as exc:
                        logger.warning("Loop %s failed to send: %s", instance.id, exc)

        except asyncio.CancelledError:
            logger.info("Loop %s cancelled", instance.id)
        except Exception:
            logger.exception("Loop %s failed", instance.id)
        finally:
            # Clean up from active list
            self._loops.pop(instance.id, None)


def get_loop_manager(app: object) -> LoopManager:
    """Get or create the LoopManager attached to an app instance."""
    if not hasattr(app, "_loop_manager"):
        app._loop_manager = LoopManager()  # type: ignore[attr-defined]
    return app._loop_manager  # type: ignore[attr-defined]


class LoopHandler(BaseCommandHandler):
    """Run an instruction periodically at a specified interval.

    Usage:
      ``/loop <interval> <instruction>`` — start a new loop
      ``/loop show``                      — list active loops
      ``/loop stop [id]``                 — stop a loop or all loops

    Intervals: ``30s``, ``5m``, ``1h``, ``1d``.
    Default: 10 minutes.  Max iterations: 100.
    """

    @property
    def name(self) -> str:
        return "/loop"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        manager = get_loop_manager(ctx.app)
        args = ctx.args.strip()

        if not args:
            return self._show(manager)

        sub = args.split()[0].lower()

        # ── show ─────────────────────────────────────────
        if sub in {"show", "list", "status"}:
            return self._show(manager)

        # ── stop [id] ────────────────────────────────────
        if sub == "stop":
            parts = args.split(maxsplit=1)
            loop_id = parts[1].strip() if len(parts) > 1 else None
            count = await manager.stop(loop_id)
            if count == 0:
                msg = f"Loop `{loop_id}` not found." if loop_id else "No active loops to stop."
                return CommandResult(success=False, message=msg)
            return CommandResult(
                success=True,
                message=f"Stopped {count} loop(s).",
            )

        # ── start <interval> <instruction> ───────────────
        parts = args.split(maxsplit=1)
        first_token = parts[0]

        # Check if first token is a valid interval
        if _INTERVAL_PATTERN.match(first_token):
            interval = first_token
            instruction = parts[1].strip() if len(parts) > 1 else ""
        else:
            # No interval — use default, entire args is instruction
            interval = None
            instruction = args

        if not instruction:
            return CommandResult(
                success=False,
                message="Usage: /loop [interval] <instruction>\n\n"
                "Examples:\n"
                "  `/loop 5m check deployment status`\n"
                "  `/loop 30s tail error logs`\n"
                "  `/loop 1h run health check`",
            )

        instance = await manager.start(
            interval, instruction, app=ctx.app
        )

        return CommandResult(
            success=True,
            message=(
                f"Loop started: `{instance.id}`\n"
                f"  Interval: {instance.interval_human}\n"
                f"  Max iterations: {instance.max_iterations}\n"
                f"  Instruction: {instruction}\n\n"
                f"Use `/loop stop {instance.id}` to cancel."
            ),
        )

    def _show(self, manager: LoopManager) -> CommandResult:
        loops = manager.all
        if not loops:
            return CommandResult(
                success=True,
                message="No active loops.\n\nUse `/loop <interval> <instruction>` to start one.",
            )

        lines = ["**Active Loops:**", ""]
        for loop in loops:
            status = "[running]" if loop.is_running else "[finished]"
            lines.append(
                f"  • `{loop.id}` {status} — every {loop.interval_human}, "
                f"iteration {loop.iteration_count}/{loop.max_iterations}\n"
                f"    _{loop.instruction[:60]}_"
            )

        return CommandResult(success=True, message="\n".join(lines))


def _parse_interval(s: str) -> int:
    """Parse an interval string to seconds."""
    match = _INTERVAL_PATTERN.match(s)
    if not match:
        return _DEFAULT_INTERVAL_SECONDS

    value = int(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers.get(unit, 60)
