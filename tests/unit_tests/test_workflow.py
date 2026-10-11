"""Unit tests for the isolated ``/workflow`` module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from opscloud.middleware.workflow import WorkflowMiddleware, _outcome_update, workflow_is_open
from opscloud.middleware.workflow_executor import _update_workflow_command, build_playbook_block
from opscloud.state.workflow_channels import _replace_or_reset_messages, is_workflow_turn
from opscloud.workflows.discovery import list_workflows, resolve_workflow, workflow_from_skill

# ── Fakes ─────────────────────────────────────────────────


class FakeAgent:
    """Minimal stand-in for the compiled nested workflow agent."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def ainvoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.invoke(payload, **kwargs)


def _runtime() -> Any:
    events: list[dict[str, Any]] = []
    return SimpleNamespace(context=None, stream_writer=events.append, events=events)


def _spec(tmp_path: Path) -> dict[str, Any]:
    skill_dir = tmp_path / "restart-pods"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: restart-pods\n---\n# Restart unhealthy pods\n1. Check pods\n2. If CrashLooping, restart\n",
        encoding="utf-8",
    )
    spec = workflow_from_skill(
        {
            "name": "k8s:restart-pods",
            "description": "Restart unhealthy pods",
            "path": str(skill_dir / "SKILL.md"),
            "source": "plugin",
            "frontmatter": {},
        }
    )
    assert spec is not None
    return spec.to_dict()


def _agent_result(*, tool_status: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "messages": [HumanMessage(content="kickoff", id="h1"), AIMessage(content="done", id="a1")],
    }
    if tool_status:
        result["workflow_tool_update"] = {"status": tool_status, "note": "evidence", "artifacts": ["/tmp/report.md"]}
    return result


def _start(tmp_path: Path, args: str = "env=prod service=checkout") -> dict[str, Any]:
    return {"kind": "start", "request_id": "r1", "workflow": _spec(tmp_path), "args": args}


# ── State channel reducer ─────────────────────────────────


def test_workflow_messages_reducer_reset_append_and_ignore() -> None:
    existing: list[Any] = [HumanMessage(content="a", id="1")]
    assert _replace_or_reset_messages(existing, {"reset": True}) == []
    replaced = _replace_or_reset_messages(existing, {"reset": True, "messages": [HumanMessage(content="b", id="2")]})
    assert [m.content for m in replaced] == ["b"]
    appended = _replace_or_reset_messages(existing, [HumanMessage(content="c", id="3")])
    assert [m.content for m in appended] == ["a", "c"]
    assert _replace_or_reset_messages(existing, {"unexpected": 1}) == existing


def test_is_workflow_turn() -> None:
    assert is_workflow_turn({"_workflow_turn": True})
    assert is_workflow_turn({"workflow_request": {"kind": "start"}})
    assert not is_workflow_turn({"messages": []})


# ── Discovery ─────────────────────────────────────────────


def test_discovery_defaults_and_flags(tmp_path: Path) -> None:
    base = {"description": "d", "path": str(tmp_path), "source": "built-in"}
    assert workflow_from_skill({**base, "name": "plain", "frontmatter": {}}) is not None
    assert workflow_from_skill({**base, "name": "off", "frontmatter": {"workflow": False}}) is None
    assert workflow_from_skill({**base, "name": "meta-off", "frontmatter": {"metadata": {"workflow": "no"}}}) is None
    assert workflow_from_skill({**base, "name": "sub", "source": "subagent", "frontmatter": {}}) is None
    assert workflow_from_skill({**base, "name": "disabled", "enabled": False, "frontmatter": {}}) is None


def test_list_and_resolve_workflows(tmp_path: Path) -> None:
    skills = [
        {"name": "k8s:restart-pods", "description": "r", "path": str(tmp_path), "source": "plugin", "frontmatter": {}},
        {"name": "web-research", "description": "w", "path": str(tmp_path), "source": "built-in", "frontmatter": {}},
        {"name": "web-research", "description": "dup", "path": str(tmp_path), "source": "user", "frontmatter": {}},
    ]
    workflows = list_workflows(skills)
    assert [w.name for w in workflows] == ["web-research", "k8s:restart-pods"]
    assert resolve_workflow("restart-pods", workflows).name == "k8s:restart-pods"  # type: ignore[union-attr]
    assert resolve_workflow("K8S:RESTART-PODS", workflows).name == "k8s:restart-pods"  # type: ignore[union-attr]
    assert resolve_workflow("missing", workflows) is None


# ── Outcome derivation ────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"workflow_tool_update": {"status": "complete", "note": "n"}}, "complete"),
        ({"workflow_tool_update": {"status": "blocked", "note": "need cluster"}}, "blocked"),
        ({"workflow_tool_update": {"status": "failed", "note": "kubectl error"}}, "failed"),
        ({"workflow_tool_update": {"status": "bogus"}}, "running"),
        ({}, "running"),
    ],
)
def test_outcome_update(result: dict[str, Any], expected: str) -> None:
    assert _outcome_update(result)["_workflow_status"] == expected


def test_outcome_update_records_note_and_artifacts() -> None:
    update = _outcome_update(_agent_result(tool_status="complete"))
    assert update["_workflow_artifacts"] == ["/tmp/report.md"]
    assert update["_workflow_status_note"] == "evidence"


# ── In-agent middleware ───────────────────────────────────


def test_update_workflow_command_requires_note() -> None:
    empty = _update_workflow_command(status="complete", note="  ", artifacts=None, tool_call_id="t")
    assert empty.update is not None and "workflow_tool_update" not in empty.update
    ok = _update_workflow_command(status="failed", note="step 2 failed", artifacts=[" /a ", ""], tool_call_id="t")
    assert ok.update is not None
    assert ok.update["workflow_tool_update"] == {"status": "failed", "note": "step 2 failed", "artifacts": ["/a"]}


def test_playbook_block_includes_skill_and_params(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    block = build_playbook_block({"name": spec["name"], "skill_path": spec["path"], "args": "env=prod"})
    assert block is not None
    assert "<workflow_definition" in block and "Restart unhealthy pods" in block
    assert "<workflow_run>" in block and "env=prod" in block
    no_args = build_playbook_block({"name": spec["name"], "skill_path": spec["path"]})
    assert no_args is not None and "(none provided)" in no_args
    assert build_playbook_block({"skill_path": ""}) is None


def test_workflow_agent_has_no_planner_or_rubric() -> None:
    import opscloud.middleware.workflow_executor as executor_mod
    import opscloud.workflows.agent as agent_mod

    assert not hasattr(agent_mod, "create_workflow_planner_agent")
    assert not hasattr(executor_mod, "WorkflowRubricMiddleware")


# ── WorkflowMiddleware dispatch ───────────────────────────


def test_no_request_is_passthrough() -> None:
    mw = WorkflowMiddleware(FakeAgent({}))
    assert mw.before_agent(cast(Any, {"messages": []}), _runtime()) is None
    assert mw.before_agent(cast(Any, {"messages": [], "_workflow_turn": True}), _runtime()) == {"_workflow_turn": False}


def test_start_runs_agent_immediately_in_isolation(tmp_path: Path) -> None:
    agent = FakeAgent(_agent_result(tool_status="complete"))
    mw = WorkflowMiddleware(agent)
    state = {"messages": [HumanMessage(content="main chat", id="m1")], "workflow_request": _start(tmp_path)}
    update = mw.before_agent(cast(Any, state), _runtime())

    assert update is not None
    assert "messages" not in update  # main transcript never touched
    assert update["jump_to"] == "end"
    assert update["_workflow_turn"] is True
    assert update["workflow_request"] is None
    assert update["_workflow_status"] == "complete"
    assert update["_workflow_run_id"] == "r1"
    assert update["_workflow_name"] == "k8s:restart-pods"
    assert update["_workflow_args"] == "env=prod service=checkout"
    assert update["_workflow_messages"]["reset"] is True

    payload = agent.calls[0]
    assert len(payload["messages"]) == 1
    kickoff = str(payload["messages"][0].content)
    assert "main chat" not in kickoff and "env=prod service=checkout" in kickoff
    assert payload["workflow_context"] == {
        "name": "k8s:restart-pods",
        "skill_path": update["_workflow_skill_path"],
        "args": "env=prod service=checkout",
    }
    assert "rubric" not in payload


def test_start_without_args_still_runs(tmp_path: Path) -> None:
    agent = FakeAgent(_agent_result())
    mw = WorkflowMiddleware(agent)
    update = mw.before_agent(cast(Any, {"messages": [], "workflow_request": _start(tmp_path, args="")}), _runtime())
    assert update is not None and update["_workflow_status"] == "running"
    assert "(none provided)" in str(agent.calls[0]["messages"][0].content)


def test_continue_uses_isolated_history() -> None:
    agent = FakeAgent(_agent_result(tool_status="blocked"))
    mw = WorkflowMiddleware(agent)
    history = [HumanMessage(content="kickoff", id="h1"), AIMessage(content="which cluster?", id="a1")]
    state = {
        "messages": [HumanMessage(content="main chat", id="m1")],
        "_workflow_name": "k8s:restart-pods",
        "_workflow_skill_path": "/tmp/SKILL.md",
        "_workflow_args": "env=prod",
        "_workflow_status": "running",
        "_workflow_messages": history,
        "workflow_request": {"kind": "continue", "request_id": "c", "message": "eu-west-1"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())
    assert update is not None and "messages" not in update
    assert update["_workflow_status"] == "blocked"
    sent = agent.calls[0]["messages"]
    assert [m.content for m in sent] == ["kickoff", "which cluster?", "eu-west-1"]
    assert agent.calls[0]["workflow_context"]["args"] == "env=prod"
    assert isinstance(update["_workflow_messages"], list)  # appended, not reset


def test_cancel_clears_run() -> None:
    agent = FakeAgent({})
    mw = WorkflowMiddleware(agent)
    update = mw.before_agent(
        cast(Any, {"messages": [], "_workflow_status": "running", "workflow_request": {"kind": "cancel", "request_id": "x"}}),
        _runtime(),
    )
    assert update is not None
    assert update["_workflow_status"] == "cancelled"
    assert update["_workflow_name"] is None
    assert update["_workflow_messages"] == {"reset": True}
    assert agent.calls == []


def test_validation_rejects_without_running_agent(tmp_path: Path) -> None:
    agent = FakeAgent({})
    mw = WorkflowMiddleware(agent)
    busy = {"messages": [], "_workflow_status": "blocked", "_workflow_name": "other", "workflow_request": _start(tmp_path)}
    update = mw.before_agent(cast(Any, busy), _runtime())
    assert update is not None and "still blocked" in update["_workflow_status_note"]
    assert "_workflow_status" not in update
    assert agent.calls == []

    no_run = {"messages": [], "workflow_request": {"kind": "continue", "request_id": "c", "message": "hi"}}
    rejected = mw.before_agent(cast(Any, no_run), _runtime())
    assert rejected is not None and "No running workflow" in rejected["_workflow_status_note"]

    legacy = {"messages": [], "workflow_request": {"kind": "plan", "request_id": "p"}}
    rejected = mw.before_agent(cast(Any, legacy), _runtime())
    assert rejected is not None and "Unknown workflow request kind" in rejected["_workflow_status_note"]

    unavailable = WorkflowMiddleware(None)
    rejected = unavailable.before_agent(cast(Any, {"messages": [], "workflow_request": _start(tmp_path)}), _runtime())
    assert rejected is not None and "unavailable" in rejected["_workflow_status_note"]


def test_agent_failure_marks_failed(tmp_path: Path) -> None:
    mw = WorkflowMiddleware(FakeAgent(RuntimeError("boom")))
    update = mw.before_agent(cast(Any, {"messages": [], "workflow_request": _start(tmp_path)}), _runtime())
    assert update is not None
    assert update["_workflow_status"] == "failed"
    assert "boom" in update["_workflow_status_note"]
    assert update["jump_to"] == "end"


def test_interrupts_bubble_up(tmp_path: Path) -> None:
    from langgraph.errors import GraphInterrupt

    mw = WorkflowMiddleware(FakeAgent(GraphInterrupt(())))
    with pytest.raises(GraphInterrupt):
        mw.before_agent(cast(Any, {"messages": [], "workflow_request": _start(tmp_path)}), _runtime())


def test_async_path_matches_sync(tmp_path: Path) -> None:
    mw = WorkflowMiddleware(FakeAgent(_agent_result(tool_status="complete")))
    runtime = _runtime()
    update = asyncio.run(mw.abefore_agent(cast(Any, {"messages": [], "workflow_request": _start(tmp_path)}), runtime))
    assert update is not None and update["_workflow_status"] == "complete"
    assert runtime.events and runtime.events[0] == {"type": "workflow_phase", "phase": "running", "name": "k8s:restart-pods"}


def test_workflow_is_open() -> None:
    assert workflow_is_open({"_workflow_status": "running"})
    assert workflow_is_open({"_workflow_status": "blocked"})
    assert not workflow_is_open({"_workflow_status": "complete"})
    assert not workflow_is_open({"_workflow_status": "failed"})
    assert not workflow_is_open({})


# ── Main-agent guards ─────────────────────────────────────


def test_main_rubric_skips_workflow_turns() -> None:
    from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware

    mw = ReliableRubricMiddleware.__new__(ReliableRubricMiddleware)
    assert mw._prepare_evaluation(cast(Any, {"_workflow_turn": True, "rubric": "- x"}), _runtime()) is None


# ── Command helpers ───────────────────────────────────────


def test_workflow_state_reply_routing() -> None:
    from opscloud.commands.power.workflow import WorkflowState

    state = WorkflowState()
    state.status = "blocked"
    assert state.accepts_replies and state.is_open
    state.attached = False
    assert not state.accepts_replies
    state.attached = True
    state.status = "complete"
    assert not state.accepts_replies and not state.is_open
    state.clear()
    assert state.status is None and state.attached
