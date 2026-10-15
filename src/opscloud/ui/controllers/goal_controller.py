"""Goal and rubric state controller extracted from `OpsCloudApp`."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from opscloud.ui.widgets.messages import AppMessage, ErrorMessage, SystemMessage
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)


class GoalController:
    """Manages `/goal` state synchronization, checkpoint persistence, and rubric evaluation."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    @asynccontextmanager
    async def goal_state_mutation_boundary(self) -> AsyncGenerator[None, None]:
        """Serialize an out-of-run goal mutation with graph checkpoints."""
        app = self._app
        async with app._goal_state_lock:
            app._goal_state_mutating = True
            try:
                while app._agent_running or app._agent_reconciling:
                    await asyncio.sleep(0.05)
                yield
            finally:
                app._goal_state_mutating = False

    async def aupdate_thread_state(self, update: dict[str, Any]) -> None:
        """Write one state update through the graph client."""
        app = self._app
        if not app._adapter or not app._adapter.connected or not app._agent_thread_id:
            return
        config: dict[str, Any] = {"configurable": {"thread_id": app._agent_thread_id}}
        client = app._adapter._client

        if hasattr(client, "aensure_thread"):
            await client.aensure_thread(config)
            await client.aupdate_state(config, update, as_node="model")
        else:
            await client.aupdate_state(config, update)

    def goal_state_update(self) -> dict[str, Any]:
        """Build checkpoint state for goal/rubric metadata managed by the TUI."""
        from opscloud.commands.power.goal import get_goal_state

        state = get_goal_state(self._app)
        return {
            "rubric": (
                None
                if state.objective and state.status in {"paused", "complete"}
                else state.rubric
            ),
            "_sticky_rubric": state.rubric,
            "_goal_objective": state.objective,
            "_goal_status": state.status if state.objective else None,
            "_goal_rubric": state.rubric if state.objective else None,
            "_goal_status_note": state.status_note if state.objective else None,
            "_pending_goal_objective": state.pending_objective,
            "_pending_goal_rubric": state.pending_rubric,
            "_pending_goal_kind": (
                state.pending_kind if state.pending_objective else None
            ),
        }

    async def persist_goal_rubric_state(
        self,
        *,
        notice: Any | None = None,
        state_update: dict[str, Any] | None = None,
    ) -> bool:
        """Persist TUI-owned goal state and an optional notice atomically."""
        app = self._app
        if not app._adapter or not app._adapter.connected or not app._agent_thread_id:
            return True
        update = dict(state_update or app._goal_state_update())
        if notice is not None:
            if hasattr(notice, "content") and hasattr(notice, "type"):
                update["messages"] = [{"role": getattr(notice, "type", "system"), "content": str(notice.content)}]
            elif isinstance(notice, dict):
                update["messages"] = [notice]
            else:
                update["messages"] = [str(notice)]
        try:
            await app._aupdate_thread_state(update)
        except Exception:
            logger.warning("Failed to persist goal/rubric state", exc_info=True)
            app.notify(
                "Could not persist goal/rubric state for this thread.",
                severity="warning",
                markup=False,
            )
            return False
        return True

    async def get_thread_state_values(self, thread_id: str | None = None) -> dict[str, Any]:
        """Fetch thread state values from the checkpoint with local SQLite fallback."""
        app = self._app
        tid = thread_id or app._agent_thread_id
        if not tid:
            return {}

        config: dict[str, Any] = {"configurable": {"thread_id": tid}}

        # ── Path 1: Remote agent ──────────────────────────────────
        if app._adapter and app._adapter.connected:
            client = app._adapter._client
            if hasattr(client, "aensure_thread"):
                try:
                    await client.aensure_thread(config)
                except Exception:
                    logger.debug("aensure_thread failed for %s, proceeding anyway", tid)

            try:
                state = await client.aget_state(config)
                if state and state.values:
                    res = dict(state.values)
                    if not res.get("_goal_objective"):
                        local_state = await app._reconstruct_state_from_local_db(tid)
                        for k, v in local_state.items():
                            if (k not in res or res[k] is None) and v is not None:
                                res[k] = v
                    return res
            except Exception:
                logger.debug(
                    "Remote aget_state failed for %s; trying local checkpointer",
                    tid,
                    exc_info=True,
                )

        # ── Path 2: Local checkpointer fallback ───────────────────
        return await app._reconstruct_state_from_local_db(tid)

    async def reconstruct_state_from_local_db(
        self,
        thread_id: str,
    ) -> dict[str, Any]:
        """Reconstruct thread state from the local SQLite checkpoint database."""
        from opscloud.state.session import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return {}

        try:
            import aiosqlite
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            serde = JsonPlusSerializer()

            async with aiosqlite.connect(db_path) as conn:
                async with conn.execute(
                    """
                    SELECT type, value
                    FROM writes
                    WHERE thread_id = ?
                      AND checkpoint_ns = ''
                      AND channel = 'messages'
                    ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                    """,
                    (thread_id,),
                ) as cursor:
                    rows: list[tuple[str, bytes]] = [
                        (r[0], r[1]) for r in await cursor.fetchall()
                    ]

            if not rows:
                return {}

            loop = asyncio.get_running_loop()
            messages = await loop.run_in_executor(
                None, self._app._reduce_message_writes, rows, serde
            )
            if messages:
                return {"messages": messages}
        except Exception:
            logger.warning(
                "Local DB reconstruction failed for thread %s",
                thread_id,
                exc_info=True,
            )

        return {}

    @staticmethod
    def reduce_message_writes(
        rows: list[tuple[str, bytes]],
        serde: Any,
    ) -> list[Any]:
        """Replay message-channel write rows through `add_messages`."""
        from langgraph.graph.message import add_messages

        accumulated: list[Any] = []

        for type_str, value_blob in rows:
            if not type_str or not value_blob:
                continue
            try:
                delta = serde.loads_typed((type_str, value_blob))
            except Exception:
                logger.warning(
                    "Failed to decode a message write row; skipping",
                    exc_info=True,
                )
                continue

            if isinstance(delta, list):
                accumulated = list(add_messages(accumulated, delta))
            else:
                accumulated = list(add_messages(accumulated, [delta]))

        return accumulated

    async def sync_goal_state_from_checkpoint(
        self,
        *,
        force: bool = False,
        proposal_request_id: str | None = None,
        allow_pending_proposal: bool = True,
    ) -> bool:
        """Refresh goal/rubric metadata from the active checkpoint."""
        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        app = self._app
        goal_state = get_goal_state(app)
        if not force and not (
            goal_state.objective
            or goal_state.rubric
            or goal_state.next_rubric
            or goal_state.status_note
            or goal_state.pending_objective
            or goal_state.pending_rubric
        ):
            return True

        try:
            state_values = await app._get_thread_state_values()
        except Exception:
            logger.warning("Failed to refresh goal/rubric state", exc_info=True)
            if force:
                if goal_state.pending_objective and goal_state.pending_rubric:
                    app._open_goal_review(
                        goal_state.pending_objective,
                        goal_state.pending_rubric,
                    )
                else:
                    await app._mount_message(
                        ErrorMessage(
                            "Acceptance criteria were generated but could not be "
                            "loaded from the thread. Run `/goal` again to retry."
                        )
                    )
            return False

        if not state_values:
            return True

        # ── Extract checkpoint goal fields ────────────────
        pending_obj = None
        pending_rubric = None
        pending_kind = None
        pending_request_id = None

        raw_pending_obj = state_values.get("_pending_goal_objective")
        if isinstance(raw_pending_obj, str) and raw_pending_obj.strip():
            pending_obj = raw_pending_obj.strip()
        raw_pending_rubric = state_values.get("_pending_goal_rubric")
        if isinstance(raw_pending_rubric, str) and raw_pending_rubric.strip():
            pending_rubric = raw_pending_rubric.strip()
        raw_pending_kind = state_values.get("_pending_goal_kind")
        if isinstance(raw_pending_kind, str) and raw_pending_kind.strip():
            pending_kind = raw_pending_kind.strip()
        raw_pending_request_id = state_values.get("_pending_goal_request_id")
        if isinstance(raw_pending_request_id, str):
            pending_request_id = raw_pending_request_id

        # ── Active goal fields from checkpoint ───────────
        raw_obj = state_values.get("_goal_objective")
        if isinstance(raw_obj, str) and raw_obj.strip():
            goal_state.objective = raw_obj.strip()
        raw_status = state_values.get("_goal_status")
        if isinstance(raw_status, str) and raw_status in {
            "active", "blocked", "complete", "paused",
        }:
            goal_state.status = raw_status  # type: ignore[assignment]
        raw_note = state_values.get("_goal_status_note")
        if isinstance(raw_note, str) and raw_note.strip():
            goal_state.status_note = raw_note.strip()
        raw_rubric = state_values.get("_goal_rubric") or state_values.get("_sticky_rubric")
        if isinstance(raw_rubric, str) and raw_rubric.strip():
            goal_state.rubric = raw_rubric.strip()

        # ── Resolve goal completion from rubric evaluation ──
        rubric_status = state_values.get("_rubric_status")
        pending_completion_note = state_values.get("_pending_goal_completion_note")
        previous_status = raw_status or goal_state.status

        if (
            (rubric_status == "satisfied" or goal_state.status == "complete")
            and goal_state.objective
            and (raw_status == "active" or goal_state.status == "complete")
        ):
            note = pending_completion_note or goal_state.status_note or "Acceptance criteria satisfied."
            goal_state.status = "complete"
            goal_state.status_note = note
            goal_state.pending_objective = None
            goal_state.pending_rubric = None
            goal_state.pending_kind = None

            from opscloud.middleware.goal_state_notice import build_goal_state_notice
            state_update = app._goal_state_update()
            state_update["_pending_goal_completion_note"] = None
            state_update["_rubric_status"] = None
            persisted = await app._persist_goal_rubric_state(
                notice=build_goal_state_notice(state_update),
                state_update=state_update,
            )
            if persisted and previous_status != "complete":
                await app._mount_message(AppMessage(f"Goal marked complete.\n\n{note}"))
        elif rubric_status == "max_iterations_reached" and pending_completion_note:
            await app._mount_message(
                ErrorMessage(
                    "Goal completion was not recorded: the iteration limit was reached "
                    "with unmet criteria. The goal remains active for resume, "
                    "amendment, retry, or clearing."
                )
            )

        # ── Gate pending proposal on request correlation ──
        discard = False
        if not allow_pending_proposal and pending_obj is not None:
            if proposal_request_id is None or pending_request_id == proposal_request_id:
                discard = True
        if (
            proposal_request_id is not None
            and pending_request_id != proposal_request_id
        ):
            discard = True

        if discard:
            pending_obj = None
            pending_rubric = None
            pending_kind = None
            pending_request_id = None
            goal_state.pending_objective = None
            goal_state.pending_rubric = None
            goal_state.pending_kind = None
        else:
            goal_state.pending_objective = pending_obj
            goal_state.pending_rubric = pending_rubric
            goal_state.pending_kind = pending_kind

        GoalHandler._sync_status_rubric(app, goal_state)

        # ── Mount review if pending proposal exists ──────
        if pending_obj and pending_rubric:
            app._open_goal_review(pending_obj, pending_rubric)

        return True

    async def sync_session_cost_from_checkpoint(self) -> None:
        """Reconcile displayed context tokens, cumulative cost, and session tokens from the checkpoint."""
        app = self._app
        if not app._agent_thread_id:
            return
        try:
            state_values = await app._get_thread_state_values(app._agent_thread_id)
        except Exception:
            logger.debug(
                "Could not load thread state while reconciling cost and token state",
                exc_info=True,
            )
            return

        cost_val = state_values.get("_session_cost_usd")
        if isinstance(cost_val, (int, float)) and math.isfinite(cost_val) and cost_val >= 0:
            app._session_cost_usd = float(cost_val)
            if app._status_bar:
                app._status_bar.set_cost(app._session_cost_usd)

        total_tokens = state_values.get("_session_total_tokens")
        if isinstance(total_tokens, int) and total_tokens >= 0:
            app._cumulative_session_tokens = total_tokens

        context_tokens = state_values.get("_context_tokens")
        if isinstance(context_tokens, int) and context_tokens >= 0:
            app._context_tokens = context_tokens
            if app._status_bar:
                display_tokens = app._cumulative_session_tokens or app._context_tokens
                if display_tokens > 0:
                    app._status_bar.set_tokens(display_tokens, context_tokens=app._context_tokens)

    async def run_goal_criteria_request(
        self,
        request: dict[str, Any],
    ) -> None:
        """Submit one typed criteria request through the normal agent stream."""
        app = self._app
        if not app._adapter or not app._adapter.connected:
            await app._mount_message(
                ErrorMessage(
                    "Goal criteria generation requires a connected server."
                )
            )
            return
        app._agent_running = True
        app._agent_worker = app.run_worker(
            app._run_agent_task(
                "",
                graph_input={
                    "messages": [],
                    "goal_criteria_request": request,
                },
            ),
            exclusive=False,
        )

    async def accept_goal_rubric(
        self,
        rubric: str,
    ) -> bool:
        """Apply accepted criteria: persist to checkpoint and start work."""
        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        app = self._app
        goal_state = get_goal_state(app)
        objective = goal_state.pending_objective
        if not objective:
            await app._mount_message(
                SystemMessage("No pending goal to accept.")
            )
            return False
        rubric = rubric.strip()
        if not rubric:
            await app._mount_message(
                SystemMessage("Cannot accept empty goal criteria.")
            )
            return False

        kind = goal_state.pending_kind or "create"
        is_amendment = kind == "amend"

        if is_amendment and (not goal_state.objective or goal_state.status == "complete"):
            await app._mount_message(
                SystemMessage(
                    "The goal is no longer active. Start a new goal with "
                    "`/goal <objective>`."
                )
            )
            return False

        # ── Persist accepted state ───────────────────────
        goal_state.objective = objective
        goal_state.rubric = rubric
        goal_state.next_rubric = None
        if not is_amendment or goal_state.status == "blocked":
            goal_state.status = "active"
            goal_state.status_note = None
        goal_state.pending_objective = None
        goal_state.pending_rubric = None
        goal_state.pending_kind = None
        GoalHandler._sync_status_rubric(app, goal_state)

        from langchain_core.messages import SystemMessage as LCSystemMessage

        notice = LCSystemMessage(content=f"Goal {'amended' if is_amendment else 'accepted'}.")
        persisted = await app._persist_goal_rubric_state(notice=notice)

        # ── User feedback message ────────────────────────
        if is_amendment:
            await app._mount_message(
                SystemMessage("Goal amended." + ("" if persisted else
                    " (could not be saved to thread)"))
            )
        else:
            await app._mount_message(
                SystemMessage(
                    "Goal accepted. It will stay active for this thread until paused, "
                    "completed, blocked, or cleared.\nUse /goal show to inspect it or "
                    "/goal clear to remove it."
                )
            )
            if not persisted:
                await app._mount_message(
                    ErrorMessage(
                        "Goal accepted for this session, but it could not be saved "
                        "to the thread."
                    )
                )

        # ── Continuation: start working toward the goal ──
        if goal_state.status == "paused":
            return True
        if not app._agent_running:
            await app._continue_goal_work(
                "amended" if is_amendment else "created",
                objective=objective,
                persisted=persisted,
            )
        return True

    async def continue_goal_work(
        self,
        transition: str,
        *,
        objective: str | None = None,
        persisted: bool = True,
    ) -> None:
        """Send one hidden command that resumes work after a goal transition."""
        from opscloud.middleware.goal_state_notice import build_goal_continuation

        continuation = build_goal_continuation(
            transition,  # type: ignore[arg-type]
            unsaved_objective=None if persisted else objective,
        )
        await self._app._send_to_agent(cast("str", continuation.content))

    def goal_rubric_payload_from_state(
        self,
        state_values: dict[str, Any],
        raw_messages: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Extract goal and rubric metadata from raw checkpoint channel values."""
        def _as_str(value: object) -> str | None:
            return value if isinstance(value, str) else None

        def _as_nonblank_str(value: object) -> str | None:
            return value if isinstance(value, str) and value.strip() else None

        goal_obj = _as_str(state_values.get("_goal_objective"))
        goal_status = _as_str(state_values.get("_goal_status"))

        if not goal_obj and raw_messages:
            for m in raw_messages:
                name = getattr(m, "name", None)
                mtype = getattr(m, "type", None)
                if name == "get_goal" or mtype == "tool":
                    c = getattr(m, "content", "")
                    if isinstance(c, str) and '"objective"' in c:
                        try:
                            parsed = json.loads(c)
                            if isinstance(parsed, dict):
                                obj = parsed.get("objective")
                                stat = parsed.get("status")
                                if isinstance(obj, str) and obj.strip():
                                    goal_obj = obj.strip()
                                if isinstance(stat, str) and stat.strip():
                                    goal_status = stat.strip()
                                break
                        except Exception:
                            pass

        return {
            "rubric": _as_str(state_values.get("rubric")),
            "sticky_rubric": _as_str(state_values.get("_sticky_rubric")),
            "sticky_rubric_recorded": "_sticky_rubric" in state_values,
            "goal_objective": goal_obj or _as_str(state_values.get("_pending_goal_objective")),
            "goal_status": goal_status or ("active" if goal_obj else None),
            "goal_rubric": _as_str(state_values.get("_goal_rubric")),
            "goal_status_note": _as_str(state_values.get("_goal_status_note")),
            "pending_goal_completion_note": _as_str(state_values.get("_pending_goal_completion_note")),
            "rubric_status": _as_str(state_values.get("_rubric_status")),
            "rubric_grading_run_id": _as_nonblank_str(state_values.get("_current_grading_run_id")),
            "pending_goal_objective": _as_str(state_values.get("_pending_goal_objective")),
            "pending_goal_rubric": _as_str(state_values.get("_pending_goal_rubric")),
            "pending_goal_kind": _as_str(state_values.get("_pending_goal_kind")),
            "pending_goal_request_id": _as_nonblank_str(state_values.get("_pending_goal_request_id")),
        }

    def sync_status_rubric(self) -> None:
        """Sync active goal and rubric fields to local GoalState and GoalStatusPanel."""
        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        app = self._app
        goal_state = get_goal_state(app)
        goal_state.objective = app._active_goal
        goal_state.status = app._goal_status or ("active" if app._active_goal else None)
        goal_state.rubric = app._active_rubric
        goal_state.status_note = app._goal_status_note
        goal_state.pending_objective = app._pending_goal_objective
        goal_state.pending_rubric = app._pending_goal_rubric
        goal_state.pending_kind = app._pending_goal_kind

        try:
            from opscloud.ui.widgets.goal_status import GoalStatusPanel
            panel = app.query_one(GoalStatusPanel)
            panel.set_goal(
                app._active_goal or app._pending_goal_objective,
                app._goal_status or ("active" if app._active_goal else None),
                app._goal_status_note or app._pending_goal_completion_note,
            )
        except Exception:
            pass

        try:
            GoalHandler._sync_status_rubric(app, goal_state)
        except Exception:
            pass

    def restore_goal_rubric_state(self, payload: dict[str, Any]) -> None:
        """Restore active goal and rubric state from thread payload."""
        app = self._app
        app._active_goal = payload.get("goal_objective")
        app._goal_status = payload.get("goal_status")
        app._goal_status_note = payload.get("goal_status_note")
        app._pending_goal_completion_note = payload.get("pending_goal_completion_note")
        if payload.get("goal_rubric"):
            app._active_rubric = payload.get("goal_rubric")
        elif payload.get("sticky_rubric_recorded"):
            app._active_rubric = payload.get("sticky_rubric")
        else:
            app._active_rubric = payload.get("rubric")
        app._pending_goal_objective = payload.get("pending_goal_objective")
        app._pending_goal_rubric = payload.get("pending_goal_rubric")
        app._pending_goal_kind = payload.get("pending_goal_kind")
        app._pending_goal_request_id = payload.get("pending_goal_request_id")
        app._sync_status_rubric()

    def handle_rubric_evaluation_end(self, rubric_msg: dict[str, Any]) -> None:
        """Handle grader evaluation completion by updating GoalState and status bar."""
        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        app = self._app
        state = get_goal_state(app)

        result = str(rubric_msg.get("result", "")).lower()
        if state.objective:
            logger.debug("Handling rubric_evaluation_end: result=%s for objective=%r", result, state.objective[:40])
            if result in ("satisfied", "passed", "complete"):
                state.status = "complete"
                if not state.status_note:
                    state.status_note = "Acceptance criteria satisfied."
            elif result in ("unsatisfied", "failed", "blocked"):
                state.status = "blocked"

        if state.next_rubric:
            state.next_rubric = None

        GoalHandler._sync_status_rubric(app, state)
        app._focus_chat_input_after_refresh()
