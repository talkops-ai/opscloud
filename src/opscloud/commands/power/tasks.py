"""``/tasks`` — Session TODO list and background task manager."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


TaskStatus = Literal["pending", "in-progress", "done"]


@dataclass
class TaskItem:
    """A single session-scoped TODO item."""

    id: int
    text: str
    status: TaskStatus = "pending"
    created_at: float = field(default_factory=time.time)


class SessionTaskStore:
    """In-memory store for session TODO tasks.

    Session-scoped — all tasks are lost when the session ends.
    This keeps things lightweight; persistent task storage belongs
    in a future persistence layer.
    """

    def __init__(self) -> None:
        self._tasks: list[TaskItem] = []
        self._next_id: int = 1

    def add(self, text: str) -> TaskItem:
        """Add a new pending task."""
        task = TaskItem(id=self._next_id, text=text)
        self._tasks.append(task)
        self._next_id += 1
        return task

    def done(self, task_id: int) -> TaskItem | None:
        """Mark a task as done. Returns the task or ``None``."""
        for task in self._tasks:
            if task.id == task_id:
                task.status = "done"
                return task
        return None

    def progress(self, task_id: int) -> TaskItem | None:
        """Mark a task as in-progress."""
        for task in self._tasks:
            if task.id == task_id:
                task.status = "in-progress"
                return task
        return None

    def remove(self, task_id: int) -> bool:
        """Remove a task entirely."""
        for i, task in enumerate(self._tasks):
            if task.id == task_id:
                self._tasks.pop(i)
                return True
        return False

    def clear_done(self) -> int:
        """Remove all completed tasks. Returns count removed."""
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.status != "done"]
        return before - len(self._tasks)

    @property
    def all(self) -> list[TaskItem]:
        return list(self._tasks)

    @property
    def pending(self) -> list[TaskItem]:
        return [t for t in self._tasks if t.status != "done"]


# Module-level singleton so the store persists across commands within a session.
_session_store = SessionTaskStore()


def get_task_store() -> SessionTaskStore:
    """Return the singleton session task store."""
    return _session_store


class TasksHandler(BaseCommandHandler):
    """Session-scoped TODO list and background task visibility.

    Subcommands:
      ``/tasks``             — list all tasks
      ``/tasks add <text>``  — add a TODO item
      ``/tasks done <id>``   — mark task complete
      ``/tasks rm <id>``     — remove a task
      ``/tasks clear``       — remove completed tasks
    """

    @property
    def name(self) -> str:
        return "/tasks"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        store = get_task_store()
        args = ctx.args.strip()

        if not args:
            return self._list_tasks(store, ctx)

        sub, _, remainder = args.partition(" ")
        sub = sub.lower()
        remainder = remainder.strip()

        if sub == "add":
            if not remainder:
                return CommandResult(success=False, message="Usage: /tasks add <description>")
            task = store.add(remainder)
            return CommandResult(
                success=True,
                message=f"Added task #{task.id}: {task.text}",
            )

        if sub == "done":
            task_id = _parse_id(remainder)
            if task_id is None:
                return CommandResult(success=False, message="Usage: /tasks done <id>")
            task = store.done(task_id)
            if task is None:
                return CommandResult(success=False, message=f"Task #{task_id} not found.")
            return CommandResult(success=True, message=f"Completed task #{task.id}: {task.text}")

        if sub in {"rm", "remove", "delete"}:
            task_id = _parse_id(remainder)
            if task_id is None:
                return CommandResult(success=False, message="Usage: /tasks rm <id>")
            if store.remove(task_id):
                return CommandResult(success=True, message=f"Removed task #{task_id}.")
            return CommandResult(success=False, message=f"Task #{task_id} not found.")

        if sub == "clear":
            count = store.clear_done()
            if count == 0:
                return CommandResult(success=True, message="No completed tasks to clear.")
            return CommandResult(success=True, message=f"Cleared {count} completed task(s).")

        # Unknown subcommand — treat first token as task text (convenience)
        task = store.add(args)
        return CommandResult(
            success=True,
            message=f"Added task #{task.id}: {task.text}",
        )

    def _list_tasks(self, store: SessionTaskStore, ctx: CommandContext) -> CommandResult:
        """Format and return the task list."""
        tasks = store.all
        if not tasks:
            return CommandResult(
                success=True,
                message="No tasks yet. Use `/tasks add <description>` to create one.",
            )

        lines = ["**Session Tasks:**", ""]
        for t in tasks:
            if t.status == "done":
                marker = "[x]"
                style = "~~"
            elif t.status == "in-progress":
                marker = "[>]"
                style = ""
            else:
                marker = "[ ]"
                style = ""

            text = f"{style}{t.text}{style}" if style else t.text
            lines.append(f"  {marker} #{t.id} {text}")

        pending = len(store.pending)
        total = len(tasks)
        done = total - pending
        lines.append("")
        lines.append(f"_{done}/{total} completed_")

        return CommandResult(success=True, message="\n".join(lines))


def _parse_id(s: str) -> int | None:
    """Parse a task ID, stripping optional '#' prefix."""
    s = s.strip().lstrip("#")
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
