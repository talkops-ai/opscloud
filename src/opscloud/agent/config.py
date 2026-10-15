"""Agent mode discovery, configuration, and default persistence for OpsCloud."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from opscloud.config.paths import STATE_DIR as _STATE_DIR
from opscloud.config.toml_config import (
    clear_default_agent as clear_default_agent,
    load_default_agent as _toml_load_default_agent,
    load_recent_agent as load_recent_agent,
    save_default_agent as save_default_agent,
    save_recent_agent as save_recent_agent,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_AGENT_FILE = _STATE_DIR / "default_agent.json"

AVAILABLE_AGENTS: list[str] = [
    "agent",
    "conversation_history",
]


def get_available_agent_names() -> list[str]:
    """Return the list of available agent modes and discovered custom agents."""
    from pathlib import Path
    from opscloud.config import paths as config_paths

    names = list(AVAILABLE_AGENTS)
    search_dirs: list[Path] = [
        config_paths.user_agents_dir(),
        Path.cwd() / ".opscloud" / "agents",
    ]
    for base in search_dirs:
        if not base.is_dir():
            continue
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name not in names:
                    if (child / "AGENTS.md").is_file():
                        names.append(child.name)
        except OSError:
            pass
    return names


def load_default_agent() -> str | None:
    """Load the persisted default agent name from config.toml or fallbacks."""
    default_agent = _toml_load_default_agent()
    if default_agent:
        return default_agent
    recent_agent = load_recent_agent()
    if recent_agent:
        return recent_agent
    try:
        if _DEFAULT_AGENT_FILE.exists():
            data = json.loads(_DEFAULT_AGENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "agent" in data:
                return str(data["agent"])
    except Exception:
        pass
    return None


@dataclass
class AgentContext:
    """Runtime context passed through the agent lifecycle."""

    model: str = ""
    thread_id: str = ""
    turn_id: str = ""
    approval_mode: str = "manual"
    working_dir: str = "."
    interactive: bool = True
    auto_approve: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(init=False)
class CLIContextSchema:
    """Standard context schema for the OpsCloud LangGraph execution graph."""

    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    model_context_limit: int | None = None
    approval_mode: str = "manual"
    auto_approve: bool = False
    smart: bool = False
    approval_mode_key: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    offload_tool_call_id: str | None = None
    hooks_snapshot_id: str | None = None
    hooks_server_events: list[str] = field(default_factory=list)
    prompt_id: str | None = None
    reasoning_effort: str | None = None
    cwd: str | None = None
    working_dir: str | None = None
    interactive: bool = True
    workspace: dict[str, Any] = field(default_factory=dict)
    aws_profile: str | None = None
    aws_region: str | None = None
    _btw_side_question: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        model: str | None = None,
        model_params: dict[str, Any] | None = None,
        profile_overrides: dict[str, Any] | None = None,
        model_context_limit: int | None = None,
        approval_mode: str = "manual",
        auto_approve: bool = False,
        smart: bool = False,
        approval_mode_key: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        offload_tool_call_id: str | None = None,
        hooks_snapshot_id: str | None = None,
        hooks_server_events: list[str] | None = None,
        prompt_id: str | None = None,
        reasoning_effort: str | None = None,
        cwd: str | None = None,
        working_dir: str | None = None,
        interactive: bool = True,
        workspace: dict[str, Any] | None = None,
        aws_profile: str | None = None,
        aws_region: str | None = None,
        _btw_side_question: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_cwd = cwd or working_dir
        ws = dict(workspace) if isinstance(workspace, dict) else {}
        if not resolved_cwd and ws and isinstance(ws.get("cwd"), str):
            resolved_cwd = str(ws["cwd"])

        self.model = model
        self.model_params = dict(model_params) if isinstance(model_params, dict) else {}
        self.profile_overrides = dict(profile_overrides) if isinstance(profile_overrides, dict) else {}
        self.model_context_limit = (
            model_context_limit
            if isinstance(model_context_limit, int) and not isinstance(model_context_limit, bool)
            else None
        )
        self.approval_mode = approval_mode or "manual"
        self.auto_approve = bool(auto_approve)
        self.smart = bool(smart)
        self.approval_mode_key = approval_mode_key
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.offload_tool_call_id = offload_tool_call_id
        self.hooks_snapshot_id = hooks_snapshot_id
        self.hooks_server_events = list(hooks_server_events) if isinstance(hooks_server_events, list) else []
        self.prompt_id = prompt_id
        self.reasoning_effort = reasoning_effort
        self.cwd = resolved_cwd
        self.working_dir = working_dir or resolved_cwd
        self.interactive = bool(interactive)
        self.workspace = ws
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self._btw_side_question = bool(_btw_side_question)
        merged_extra = dict(extra) if isinstance(extra, dict) else {}
        if kwargs:
            merged_extra.update(kwargs)
        self.extra = merged_extra

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible lookup across schema attributes and extra payload."""
        if hasattr(self, key):
            val = getattr(self, key)
            return default if val is None else val
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra[key]

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return (hasattr(self, key) and getattr(self, key) is not None) or (key in self.extra)

    @classmethod
    def from_payload(cls, payload: object) -> CLIContextSchema | None:
        """Coerce a run's `context=` payload into this schema."""
        if isinstance(payload, cls):
            return payload
        if hasattr(payload, "__dict__") and not isinstance(payload, dict):
            payload = dict(payload.__dict__)
        if not isinstance(payload, dict):
            return None
        data = dict(payload)

        def _str(key: str) -> str | None:
            val = data.get(key)
            return val if isinstance(val, str) else None

        def _mapping(key: str) -> dict[str, Any]:
            val = data.get(key)
            return dict(val) if isinstance(val, dict) else {}

        raw_limit = data.get("model_context_limit")
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else None

        raw_events = data.get("hooks_server_events")
        events = [e for e in raw_events if isinstance(e, str)] if isinstance(raw_events, list) else []

        raw_auto = data.get("auto_approve")
        auto_approve = raw_auto if isinstance(raw_auto, bool) else False

        raw_mode = _str("approval_mode")
        raw_smart = data.get("smart")
        smart = raw_smart if isinstance(raw_smart, bool) else (raw_mode == "smart" if raw_mode else False)
        if not raw_mode:
            if smart:
                raw_mode = "smart"
            elif auto_approve:
                raw_mode = "auto"
            else:
                raw_mode = "manual"

        cwd = _str("cwd") or _str("working_dir")
        workspace = _mapping("workspace")
        if not cwd and workspace and isinstance(workspace.get("cwd"), str):
            cwd = str(workspace["cwd"])

        known_keys = {
            "model",
            "model_params",
            "profile_overrides",
            "model_context_limit",
            "approval_mode",
            "auto_approve",
            "smart",
            "approval_mode_key",
            "thread_id",
            "turn_id",
            "offload_tool_call_id",
            "hooks_snapshot_id",
            "hooks_server_events",
            "prompt_id",
            "reasoning_effort",
            "cwd",
            "working_dir",
            "interactive",
            "workspace",
            "aws_profile",
            "aws_region",
            "_btw_side_question",
            "extra",
        }
        extra = _mapping("extra")
        for k, v in data.items():
            if k not in known_keys:
                extra[k] = v

        raw_interactive = data.get("interactive")
        interactive = raw_interactive if isinstance(raw_interactive, bool) else True

        return cls(
            model=_str("model"),
            model_params=_mapping("model_params"),
            profile_overrides=_mapping("profile_overrides"),
            model_context_limit=limit,
            approval_mode=raw_mode,
            auto_approve=auto_approve,
            smart=smart,
            approval_mode_key=_str("approval_mode_key"),
            thread_id=_str("thread_id"),
            turn_id=_str("turn_id"),
            offload_tool_call_id=_str("offload_tool_call_id"),
            hooks_snapshot_id=_str("hooks_snapshot_id"),
            hooks_server_events=events,
            prompt_id=_str("prompt_id"),
            reasoning_effort=_str("reasoning_effort"),
            cwd=cwd,
            working_dir=_str("working_dir") or cwd,
            interactive=interactive,
            workspace=workspace,
            aws_profile=_str("aws_profile"),
            aws_region=_str("aws_region"),
            _btw_side_question=bool(data.get("_btw_side_question")),
            extra=extra,
        )


AgentContextSchema = CLIContextSchema

__all__ = [
    "AVAILABLE_AGENTS",
    "AgentContext",
    "AgentContextSchema",
    "CLIContextSchema",
    "clear_default_agent",
    "get_available_agent_names",
    "load_default_agent",
    "load_recent_agent",
    "save_default_agent",
    "save_recent_agent",
]
