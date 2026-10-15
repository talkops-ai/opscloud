"""``AgentSessionMiddleware`` — run an active plugin subagent on the session.

When an agent persona is activated (e.g. via ``/agent:<name>`` or ``/agent <name>``),
the TUI submits an ``agent_session_request`` through the agent stream.
This middleware handles it in ``before_agent`` and jumps to ``"end"`` so the main
deep agent never runs and its transcript is never touched:

* ``start``    → activates the persona in graph state and optionally executes an
                 initial prompt with the compiled subagent
* ``continue`` → appends the operator message to ``_agent_session_messages`` and runs
                 the compiled subagent
* ``clear``    → resets the active agent persona and returns control to main deep agent

The subagent transcript is persisted in the private ``_agent_session_messages`` channel,
keeping it invisible to the main coordinator agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

from opscloud.middleware.registry import register_middleware
from opscloud.state.agent_session_channels import AgentSessionChannels
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

AGENT_SESSION_NODE_PREFIX = "AgentSessionMiddleware."
"""Stream namespace prefix for nested agent session events (used by TextualAgentAdapter)."""


def _cleared_agent_session() -> dict[str, Any]:
    return {
        "_active_agent_id": None,
        "_active_agent_plugin_id": None,
        "_active_agent_description": None,
        "_agent_session_messages": {"reset": True},
    }


@register_middleware(name="agent_session")
class AgentSessionMiddleware(AgentMiddleware[AgentSessionChannels, Any]):
    """Dispatch typed agent session requests to the compiled plugin subagent."""

    state_schema = AgentSessionChannels

    def __init__(self, agent_runnables: dict[str, Any] | None = None) -> None:
        """Initialize with mapping of agent names to their compiled subagent graphs.

        Args:
            agent_runnables: Mapping of agent names/short names to compiled runnables.
        """
        super().__init__()
        self._agents: dict[str, Any] = dict(agent_runnables or {})

    def register_agent(self, name: str, runnable: Any) -> None:
        """Register or override a compiled subagent runnable."""
        self._agents[name] = runnable
        short = name.rsplit(":", 1)[-1]
        if "@" in short:
            short = short.split("@", 1)[0]
        self._agents[short] = runnable
        self._agents[name.lower()] = runnable
        self._agents[short.lower()] = runnable

    def _resolve_agent(self, name: str) -> Any | None:
        """Look up runnable by exact name, short name, or case-insensitive."""
        if not name:
            return None
        candidate = self._agents.get(name)
        if candidate is not None:
            return candidate
        clean = name.strip()
        candidate = self._agents.get(clean.lower())
        if candidate is not None:
            return candidate
        short = clean.rsplit(":", 1)[-1]
        if "@" in short:
            short = short.split("@", 1)[0]
        candidate = self._agents.get(short)
        if candidate is not None:
            return candidate
        return self._agents.get(short.lower())

    @staticmethod
    def _finish(update: dict[str, Any]) -> dict[str, Any]:
        return {
            **update,
            "agent_session_request": None,
            "_agent_session_turn": True,
            "jump_to": "end",
        }

    # ── operations ───────────────────────────────────────

    def _execute_turn(
        self,
        request: dict[str, Any],
        values: dict[str, Any],
        runtime: Runtime[Any],
        is_async: bool = False,
    ) -> Any:
        kind = request.get("kind")
        if kind == "clear":
            return self._finish(_cleared_agent_session())

        agent_name = str(request.get("agent_name") or values.get("_active_agent_id") or "").strip()
        plugin_id = request.get("plugin_id") or values.get("_active_agent_plugin_id")
        agent_runnable = self._resolve_agent(agent_name)

        excluded_state_keys = {
            "messages",
            "agent_session_request",
            "_agent_session_messages",
            "_agent_session_turn",
            "_agent_session_error",
            "_active_agent_id",
            "_active_agent_plugin_id",
            "_active_agent_description",
            # Exclude goal/rubric and large skill cache channels
            # so they do not bloat or leak into the isolated subagent checkpoint state.
            "goal_criteria_request",
            "_goal_objective",
            "_goal_status",
            "_goal_rubric",
            "_goal_status_note",
            "_pending_goal_objective",
            "_pending_goal_rubric",
            "_pending_goal_kind",
            "_pending_goal_request_id",
            "_pending_goal_completion_note",
            "rubric",
            "_sticky_rubric",
            "_rubric_status",
            "_current_grading_run_id",
            "skills_metadata",
        }
        inherited_state = {
            k: v for k, v in values.items()
            if k not in excluded_state_keys and v is not None
        }

        if kind == "start":
            prompt = request.get("prompt")
            base = {
                "_active_agent_id": agent_name,
                "_active_agent_plugin_id": plugin_id,
            }
            if not prompt:
                # Activation only, no LLM call yet
                return self._finish({
                    **base,
                    "_agent_session_messages": {"reset": True},
                })

            if agent_runnable is None:
                logger.warning("Plugin agent runnable for '%s' not found", agent_name)
                return self._finish(base)

            input_data = {**inherited_state, "messages": [HumanMessage(content=str(prompt))]}
            context = getattr(runtime, "context", None)
            if is_async:
                # Caller will await
                return self._async_call_subagent(agent_runnable, input_data, context, base, reset=True)
            result = agent_runnable.invoke(input_data, context=context)
            messages = result.get("messages", [HumanMessage(content=str(prompt))])
            update = {
                **base,
                "_agent_session_messages": {"reset": True, "messages": list(messages)},
            }
            transfers = result.get("_session_cost_transfers")
            if isinstance(transfers, dict) and transfers:
                update["_session_cost_transfers"] = transfers
            return self._finish(update)

        if kind == "continue":
            message = str(request.get("message") or "")
            base = {
                "_active_agent_id": agent_name,
                "_active_agent_plugin_id": plugin_id,
            }
            if agent_runnable is None:
                logger.warning("Plugin agent runnable for '%s' not found", agent_name)
                return self._finish(base)

            prev_active = values.get("_active_agent_id")
            force_reset = bool(request.get("reset")) or (
                bool(agent_name) and prev_active != agent_name
            )
            history = [] if force_reset else list(values.get("_agent_session_messages") or [])
            new_msg = HumanMessage(content=message)
            input_data = {**inherited_state, "messages": [*history, new_msg]}
            context = getattr(runtime, "context", None)
            if is_async:
                return self._async_call_subagent(
                    agent_runnable, input_data, context, base, reset=force_reset
                )
            result = agent_runnable.invoke(input_data, context=context)
            messages = list(result.get("messages", [*history, new_msg]))
            update = {
                **base,
                "_agent_session_messages": (
                    {"reset": True, "messages": messages} if force_reset else messages
                ),
            }
            transfers = result.get("_session_cost_transfers")
            if isinstance(transfers, dict) and transfers:
                update["_session_cost_transfers"] = transfers
            return self._finish(update)

        logger.warning("Unknown agent_session_request kind: %r", kind)
        return self._finish({})

    async def _async_call_subagent(
        self,
        agent_runnable: Any,
        input_data: dict[str, Any],
        context: Any,
        base: dict[str, Any],
        reset: bool,
    ) -> dict[str, Any]:
        result = await agent_runnable.ainvoke(input_data, context=context)
        raw_msgs = result.get("messages")
        messages = list(raw_msgs) if isinstance(raw_msgs, list) else list(input_data["messages"])
        update = {
            **base,
            "_agent_session_messages": {"reset": True, "messages": messages} if reset else messages,
        }
        transfers = result.get("_session_cost_transfers")
        if isinstance(transfers, dict) and transfers:
            update["_session_cost_transfers"] = transfers
        return self._finish(update)

    @staticmethod
    def _passthrough_cleanup(values: dict[str, Any]) -> dict[str, Any] | None:
        """Clean up transient flags and any orphaned agent session messages on main agent turns."""
        early: dict[str, Any] = {}
        if values.get("_agent_session_turn"):
            early["_agent_session_turn"] = False
        if not values.get("_active_agent_id") and values.get("_agent_session_messages"):
            early["_agent_session_messages"] = {"reset": True}
        return early or None

    @staticmethod
    async def _close_subagent_mcp_if_needed(
        request: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        """Close active subagent MCP stdio sessions when clearing or switching agents."""
        kind = request.get("kind")
        prev_active = values.get("_active_agent_id")
        target_agent = request.get("agent_name")
        if kind == "clear":
            to_close: str | None = None
        elif prev_active and target_agent and prev_active != target_agent:
            to_close = str(prev_active)
        elif request.get("reset"):
            to_close = str(prev_active) if prev_active else None
        else:
            return

        try:
            from opscloud.mcp.session_manager import MCPSessionManager

            if MCPSessionManager._instance is not None:
                await MCPSessionManager._instance.close_subagent_sessions(to_close)
        except Exception:
            logger.debug("Failed closing subagent MCP sessions", exc_info=True)

    # ── hooks ────────────────────────────────────────────

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: AgentSessionChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending agent session request synchronously."""
        values: dict[str, Any] = dict(state)
        request = values.get("agent_session_request")
        if not isinstance(request, dict):
            return self._passthrough_cleanup(values)
        try:
            return self._execute_turn(request, values, runtime, is_async=False)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception("Agent session request failed: %s", exc)
            return self._finish({"_agent_session_error": str(exc)})

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: AgentSessionChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending agent session request asynchronously."""
        values: dict[str, Any] = dict(state)
        request = values.get("agent_session_request")
        if not isinstance(request, dict):
            return self._passthrough_cleanup(values)
        try:
            await self._close_subagent_mcp_if_needed(request, values)
            res = self._execute_turn(request, values, runtime, is_async=True)
            if hasattr(res, "__await__"):
                return await res
            return res
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception("Agent session async request failed: %s", exc)
            return self._finish({"_agent_session_error": str(exc)})


__all__ = [
    "AGENT_SESSION_NODE_PREFIX",
    "AgentSessionMiddleware",
]
