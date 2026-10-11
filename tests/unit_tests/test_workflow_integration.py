"""Integration: WorkflowMiddleware inside a real agent graph with a checkpointer."""

from __future__ import annotations

from typing import Any, cast

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from opscloud.middleware.workflow import WorkflowMiddleware
from opscloud.middleware.workflow_executor import (
    WorkflowAgentState,
    WorkflowPlaybookMiddleware,
    WorkflowToolsMiddleware,
)
from opscloud.state.base import OpsCloudAgentState


class _ScriptedModel(BaseChatModel):
    """Fake chat model that replays a fixed list of responses."""

    responses: list[AIMessage]
    seen_system: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if messages and getattr(messages[0], "type", "") == "system":
            self.seen_system.append(str(messages[0].content))
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


class _ExplodingModel(_ScriptedModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("main agent model must not run on workflow turns")


class _FakeWorkflowAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(payload)
        return {
            "messages": [*payload["messages"], AIMessage(content="Which cluster?")],
            "workflow_tool_update": {"status": "blocked", "note": "need cluster", "artifacts": []},
        }

    def invoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


def _main_graph(workflow_agent: Any) -> Any:
    return create_agent(
        model=_ExplodingModel(responses=[]),
        middleware=[WorkflowMiddleware(workflow_agent=workflow_agent)],
        state_schema=OpsCloudAgentState,
        checkpointer=InMemorySaver(),
    )


def _start(skill_path: str, args: str = "env=prod") -> Any:
    workflow = {"name": "k8s:restart-pods", "path": skill_path, "description": "Restart pods"}
    return cast(Any, {"messages": [], "workflow_request": {"kind": "start", "request_id": "r1", "workflow": workflow, "args": args}})


async def test_workflow_turns_are_isolated_from_main_transcript(tmp_path: Any) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: restart-pods\n---\n# Runbook\n", encoding="utf-8")
    agent = _FakeWorkflowAgent()
    graph = _main_graph(agent)
    config: RunnableConfig = {"configurable": {"thread_id": "t1"}}

    await graph.ainvoke(_start(str(skill)), config)
    values = (await graph.aget_state(config)).values
    assert values.get("messages") == []
    assert values["_workflow_status"] == "blocked"
    assert values["_workflow_args"] == "env=prod"
    assert values.get("workflow_request") is None
    assert [m.content for m in values["_workflow_messages"]][-1] == "Which cluster?"

    await graph.ainvoke(
        cast(Any, {"messages": [], "workflow_request": {"kind": "continue", "request_id": "c1", "message": "eu-west-1"}}),
        config,
    )
    values = (await graph.aget_state(config)).values
    assert values.get("messages") == []
    sent = agent.calls[-1]["messages"]
    assert isinstance(sent[-1], HumanMessage) and sent[-1].content == "eu-west-1"
    assert len(sent) == 3  # kickoff, agent question, operator reply
    assert len(values["_workflow_messages"]) == 4


async def test_hitl_approval_inside_workflow_agent_resumes(tmp_path: Any) -> None:
    """A gated tool (e.g. a plugin MCP tool) interrupts for approval and resumes."""
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: restart-pods\n---\n# Runbook\n1. Restart the deployment\n", encoding="utf-8")
    executed: list[str] = []

    @tool
    def restart_deployment(name: str) -> str:
        """Restart a Kubernetes deployment."""
        executed.append(name)
        return f"restarted {name}"

    model = _ScriptedModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "restart_deployment", "args": {"name": "checkout"}, "id": "t1"}]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_workflow",
                        "args": {"status": "complete", "note": "restarted checkout", "artifacts": []},
                        "id": "t2",
                    }
                ],
            ),
            AIMessage(content="Restarted `checkout`."),
        ],
        seen_system=[],
    )
    workflow_agent = create_agent(
        model=model,
        tools=[restart_deployment],
        middleware=cast(
            Any,
            [
                HumanInTheLoopMiddleware(interrupt_on={"restart_deployment": True}),
                WorkflowPlaybookMiddleware(),
                WorkflowToolsMiddleware(),
            ],
        ),
        state_schema=WorkflowAgentState,
    )
    graph = _main_graph(workflow_agent)
    config: RunnableConfig = {"configurable": {"thread_id": "t-hitl"}}

    first = await graph.ainvoke(_start(str(skill), args="service=checkout"), config)
    assert first.get("__interrupt__"), "gated tool must interrupt for approval"
    assert executed == []  # nothing ran before approval
    assert any("<workflow_definition" in s and "service=checkout" in s for s in model.seen_system)

    await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    values = (await graph.aget_state(config)).values
    assert executed == ["checkout"]
    assert values.get("messages") == []  # main transcript still untouched
    assert values["_workflow_status"] == "complete"
    assert values["_workflow_status_note"] == "restarted checkout"
    assert any(isinstance(m, ToolMessage) and m.content == "restarted checkout" for m in values["_workflow_messages"])
