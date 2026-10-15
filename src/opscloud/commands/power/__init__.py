"""Power user command handlers package."""

from opscloud.commands.power.agents import AgentsHandler
from opscloud.commands.power.btw import BtwHandler
from opscloud.commands.power.copy import CopyHandler
from opscloud.commands.power.goal import GoalHandler
from opscloud.commands.power.loop import LoopHandler
from opscloud.commands.power.memory import MemoryHandler
from opscloud.commands.power.review import ReviewHandler
from opscloud.commands.power.rubric import RubricHandler
from opscloud.commands.power.runtime import (
    AutoUpdateHandler,
    InstallHandler,
    ReloadHandler,
    RestartHandler,
    UpdateHandler,
)
from opscloud.commands.power.skill_creator import SkillCreatorHandler
from opscloud.commands.power.skill_invoke import SkillInvokeHandler
from opscloud.commands.power.tasks import TasksHandler
from opscloud.commands.power.trace import TraceHandler
from opscloud.commands.power.ui_toggles import (
    NotificationsHandler,
    ScrollbarHandler,
    TimestampsHandler,
)
from opscloud.commands.power.version import VersionHandler

__all__ = [
    "AgentsHandler",
    "AutoUpdateHandler",
    "BtwHandler",
    "CopyHandler",
    "GoalHandler",
    "InstallHandler",
    "LoopHandler",
    "MemoryHandler",
    "NotificationsHandler",
    "ReloadHandler",
    "RestartHandler",
    "ReviewHandler",
    "RubricHandler",
    "ScrollbarHandler",
    "SkillCreatorHandler",
    "SkillInvokeHandler",
    "TasksHandler",
    "TimestampsHandler",
    "TraceHandler",
    "UpdateHandler",
    "VersionHandler",
]
