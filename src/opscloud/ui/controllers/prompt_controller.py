"""Inline prompt controller for tool approvals, ask_user questions, and goal reviews.

Inspired by Antigravity CLI's ``store/tool_confirmation_manager.go`` and
``store/ask_question_manager.go``, this controller extracts inline prompt
mounting, lifecycle management, focus restoration, interrupt cancellation, and
scoped key routing out of ``OpsCloudApp``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import TYPE_CHECKING, Any

from textual.css.query import NoMatches
from textual.widgets import Static

from opscloud.ui.widgets.approval import ApprovalDecided, ApprovalMenu
from opscloud.ui.widgets.ask_user import AskUserMenu
from opscloud.ui.widgets.goal_review import GoalReviewMenu
from opscloud.ui.widgets.messages import MessageList, SystemMessage
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from textual import events

    from opscloud.ui.app import OpsCloudApp
    from opscloud.ui.widgets._ask_user_types import Question

logger = get_logger(__name__)


class PromptController:
    """Manages inline prompts (`ApprovalMenu`, `AskUserMenu`, `GoalReviewMenu`)."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app
        self.pending_approval_widget: ApprovalMenu | Any | None = None
        self.approval_placeholder: Any | None = None
        self.pending_ask_user_widget: AskUserMenu | None = None
        self.pending_goal_review_widget: GoalReviewMenu | Any | None = None
        self.pending_goal_review_future: Any | None = None

    # ── Query & Focus Helpers ────────────────────────────

    @property
    def has_active_prompt(self) -> bool:
        """Whether any inline prompt is currently active."""
        return (
            self.pending_approval_widget is not None
            or self.pending_ask_user_widget is not None
            or self.pending_goal_review_widget is not None
        )

    def focus_chat_input_after_refresh(self) -> None:
        """Restore chat input focus after an inline prompt finishes, unless another prompt is active."""
        if self.pending_approval_widget is not None and getattr(
            self.pending_approval_widget, "is_mounted", False
        ):
            self._app.call_after_refresh(self.pending_approval_widget.focus)
            return
        if self.pending_ask_user_widget is not None and getattr(
            self.pending_ask_user_widget, "is_mounted", False
        ):
            self._app.call_after_refresh(self.pending_ask_user_widget.focus_active)
            return
        if self.pending_goal_review_widget is not None and getattr(
            self.pending_goal_review_widget, "is_mounted", False
        ):
            self._app.call_after_refresh(self.pending_goal_review_widget.focus)
            return

        chat_input = getattr(self._app, "_chat_input", None)
        if chat_input is not None:
            self._app.call_after_refresh(chat_input.focus_input)
            self._app.set_timer(0.05, self._refocus_input_if_no_prompt)

    def _refocus_input_if_no_prompt(self) -> None:
        """Deferred timer callback that only focuses ChatInput if no inline prompt mounted in the meantime."""
        if self.has_active_prompt:
            return
        chat_input = getattr(self._app, "_chat_input", None)
        if chat_input is not None:
            chat_input.focus_input()

    # ── Scoped Key & Interrupt Routing ───────────────────

    def route_key(self, event: events.Key) -> bool:
        """Route a keystroke to the active inline approval prompt if mounted and visible.

        Returns True when the key event was handled and stopped.
        """
        pending = self.pending_approval_widget
        if (
            pending is None
            or not getattr(pending, "is_mounted", False)
            or not getattr(pending, "display", True)
        ):
            return False

        key = event.key
        handled = False
        if hasattr(pending, "handle_scoped_key"):
            handled = bool(pending.handle_scoped_key(key))
        else:
            # Fallback for lightweight mock objects in unit tests
            handled = self._dispatch_fallback_approval_key(pending, key)

        if handled:
            event.prevent_default()
            event.stop()
            return True
        return False

    @staticmethod
    def _dispatch_fallback_approval_key(pending: Any, key: str) -> bool:
        """Fallback key dispatch for mock approval widgets without `handle_scoped_key`."""
        if key in ("up", "k"):
            pending.action_move_up()
            return True
        if key in ("down", "j"):
            pending.action_move_down()
            return True
        if key == "enter":
            pending.action_select()
            return True
        if key == "y":
            pending.action_select_approve()
            return True
        if key in ("s", "a"):
            if hasattr(pending, "action_select_smart"):
                pending.action_select_smart()
            else:
                pending.action_select_auto()
            return True
        if key in ("n", "escape"):
            pending.action_select_reject()
            return True
        if key in ("1", "2", "3"):
            pending.action_select_position(int(key) - 1)
            return True
        if key == "e":
            pending.action_toggle_expand()
            return True
        if key == "tab":
            pending.action_reject_with_reason()
            return True
        return not getattr(pending, "_reason_input_active", False)

    def interrupt_active_prompt(self) -> bool:
        """Reject or cancel the active approval or ask_user prompt on Esc / Ctrl+C.

        Returns True if an active inline prompt consumed the interrupt.
        """
        if self.pending_approval_widget is not None:
            self.pending_approval_widget.action_select_reject()
            return True
        if self.pending_ask_user_widget is not None:
            self.pending_ask_user_widget.action_cancel()
            return True
        return False

    # ── HITL Tool Approval Lifecycle ─────────────────────

    async def request_approval(
        self,
        action_requests: Any,
        assistant_id: str | None = None,
    ) -> asyncio.Future[Any]:
        """Request user approval inline in the messages area."""
        app = self._app
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[Any] = loop.create_future()

        is_auto_fallback = any(
            isinstance(request.get("description"), str)
            and request["description"].startswith("Auto human fallback ")
            for request in action_requests or []
            if isinstance(request, dict)
        )
        shell_allow_list = getattr(app, "_shell_allow_list", None)
        if shell_allow_list and action_requests and not is_auto_fallback:
            from opscloud.security.shell_safety import is_shell_command_allowed

            all_auto_approved = True
            approved_commands: list[str] = []

            for req in action_requests:
                if isinstance(req, dict) and req.get("name") in {
                    "execute",
                    "run_command",
                    "bash",
                    "shell",
                }:
                    command = req.get("args", {}).get("command", "") or req.get(
                        "args", {}
                    ).get("CommandLine", "")
                    if is_shell_command_allowed(command, shell_allow_list):
                        approved_commands.append(command)
                    else:
                        all_auto_approved = False
                        break
                else:
                    all_auto_approved = False
                    break

            if all_auto_approved and approved_commands:
                result_future.set_result({"type": "approve"})
                try:
                    messages = app.query_one("#messages", MessageList)
                    for command in approved_commands:
                        auto_msg = SystemMessage(
                            f"✓ Auto-approved shell command (allow-list): {command}"
                        )
                        await messages.mount(auto_msg)
                except Exception:
                    pass
                return result_future

        # Wait if another approval is already pending
        while self.pending_approval_widget is not None:
            await asyncio.sleep(0.05)

        app._pause_loading_spinner_for_approval()
        result_future.add_done_callback(app._resume_loading_spinner_after_approval)

        unique_id = f"approval-menu-{uuid.uuid4().hex[:8]}"
        menu = ApprovalMenu(
            action_requests,
            assistant_id or app._assistant_id,
            id=unique_id,
            auto_mode_eligible=not getattr(app, "_sandbox_active", False),
        )
        menu.set_future(result_future)
        self.pending_approval_widget = menu

        if app._is_user_typing():
            placeholder = Static(
                "Waiting for typing to finish...",
                classes="approval-placeholder",
            )
            self.approval_placeholder = placeholder
            try:
                messages = app.query_one("#messages", MessageList)
                res = (
                    messages.mount_inline_prompt(placeholder)
                    if hasattr(messages, "mount_inline_prompt")
                    else messages.mount(placeholder)
                )
                if inspect.isawaitable(res):
                    await res
                app.call_after_refresh(placeholder.scroll_visible)
            except Exception:
                logger.exception("Failed to mount approval placeholder")
                self.approval_placeholder = None
                await self.mount_approval_widget(menu, result_future)
                return result_future

            app.run_worker(
                self.deferred_show_approval(placeholder, menu, result_future),
                exclusive=False,
            )
        else:
            await self.mount_approval_widget(menu, result_future)

        return result_future

    async def mount_approval_widget(
        self,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Mount the approval menu widget inline in the messages area."""
        app = self._app
        try:
            messages = app.query_one("#messages", MessageList)
            res = (
                messages.mount_inline_prompt(menu)
                if hasattr(messages, "mount_inline_prompt")
                else messages.mount(menu)
            )
            if inspect.isawaitable(res):
                await res
            app.call_after_refresh(menu.scroll_visible)
            app.call_after_refresh(menu.focus)
        except Exception as e:
            logger.exception(
                "Failed to mount approval menu (id=%s) in messages container",
                menu.id,
            )
            self.pending_approval_widget = None
            if not result_future.done():
                result_future.set_exception(e)

    async def deferred_show_approval(
        self,
        placeholder: Any,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Wait until user is idle, then swap placeholder for real menu."""
        app = self._app
        deadline = time.monotonic() + 3.0
        while app._is_user_typing():
            if time.monotonic() > deadline:
                logger.warning(
                    "Timed out waiting for user to stop typing; showing approval now"
                )
                break
            await asyncio.sleep(0.2)

        if not getattr(placeholder, "is_attached", False):
            logger.warning(
                "Approval placeholder detached before menu shown (id=%s)", menu.id
            )
            self.approval_placeholder = None
            self.pending_approval_widget = None
            if not result_future.done():
                result_future.cancel()
            return

        self.approval_placeholder = None
        try:
            await placeholder.remove()
        except Exception:
            logger.warning(
                "Failed to remove approval placeholder during swap", exc_info=True
            )
        await self.mount_approval_widget(menu, result_future)

    async def on_approval_menu_decided(self, event: ApprovalMenu.Decided) -> None:
        """Handle an inline `ApprovalMenu.Decided` event."""
        app = self._app
        if self.approval_placeholder is not None:
            if getattr(self.approval_placeholder, "is_attached", False):
                try:
                    await self.approval_placeholder.remove()
                except Exception:
                    pass
            self.approval_placeholder = None

        if self.pending_approval_widget is not None:
            try:
                await self.pending_approval_widget.remove()
            except Exception:
                pass
            self.pending_approval_widget = None
        elif isinstance(event.control, ApprovalMenu):
            try:
                await event.control.remove()
            except Exception:
                pass

        decision = event.decision
        dec_type = decision.get("type")

        if dec_type in {"smart_approve_all", "auto_approve_all"}:
            from opscloud.approval_mode import ApprovalMode

            await app._set_approval_mode(ApprovalMode.SMART)

        if app._adapter and event.call_id:
            app._adapter.submit_approval(event.call_id, event.approved)

        if not event.approved and hasattr(app, "_permission_store") and event.call_id:
            app._permission_store.track_denied(
                tool_name=event.tool_name,
                call_id=event.call_id,
                comment=event.comment,
            )

        self.focus_chat_input_after_refresh()

    def on_approval_decided(self, event: ApprovalDecided) -> None:
        """Handle modal `ApprovalDecided` event."""
        app = self._app
        if app._adapter:
            app._adapter.submit_approval(event.call_id, event.approved)
        if not event.approved and hasattr(app, "_permission_store"):
            app._permission_store.track_denied(
                tool_name=event.tool_name,
                call_id=event.call_id,
                comment=event.comment,
            )
        self.focus_chat_input_after_refresh()

    async def on_interrupt_raised(self, event: Any) -> None:
        """Handle legacy `TextualAdapter.InterruptRaised` event."""
        app = self._app
        if app._auto_approve and app._adapter:
            app._adapter.submit_approval(event.call_id, True)
            return

        future = await self.request_approval(
            [{"name": event.tool_name, "call_id": event.call_id, "args": event.args}],
            app._assistant_id,
        )
        decision = await future
        if app._adapter:
            approved = decision.get("type") in {
                "approve",
                "auto_approve_all",
                "smart_approve_all",
            }
            app._adapter.submit_approval(event.call_id, approved)

    # ── Ask-User Question Lifecycle ──────────────────────

    async def request_ask_user(
        self,
        questions: list[Any],
    ) -> asyncio.Future[Any]:
        """Mount the interactive `AskUserMenu` widget and return a Future awaiting response."""
        app = self._app
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[Any] = loop.create_future()

        if self.pending_ask_user_widget is not None:
            try:
                self.pending_ask_user_widget.remove()
            except Exception:
                pass
            self.pending_ask_user_widget = None

        unique_id = f"ask-user-{uuid.uuid4().hex[:8]}"
        menu = AskUserMenu(questions, id=unique_id)
        menu.set_future(result_future)
        self.pending_ask_user_widget = menu

        try:
            messages = app.query_one("#messages", MessageList)
            res = (
                messages.mount_inline_prompt(menu)
                if hasattr(messages, "mount_inline_prompt")
                else messages.mount(menu)
            )
            if inspect.isawaitable(res):
                await res
            app.call_after_refresh(menu.scroll_visible)
            app.call_after_refresh(menu.focus_active)
        except Exception as exc:
            logger.exception("Failed to mount AskUserMenu widget: %s", exc)
            self.pending_ask_user_widget = None
            if not result_future.done():
                result_future.set_result({"type": "cancelled"})
            return result_future

        return result_future

    async def present_ask_user(self, questions: list[Question]) -> list[str] | None:
        """Mount `AskUserMenu` and await its resolved answers list (or None on cancel)."""
        future = await self.request_ask_user(questions)
        try:
            result = await future
        except Exception:
            return None
        finally:
            await self.finish_ask_user_prompt(context="present_ask_user")

        if isinstance(result, dict) and result.get("type") == "answered":
            return result.get("answers")
        return None

    async def finish_ask_user_prompt(self, *, context: str = "finished") -> None:
        """Remove the `AskUserMenu` widget and return focus to chat input."""
        if self.pending_ask_user_widget is not None:
            try:
                await self.pending_ask_user_widget.remove()
            except Exception:
                pass
            self.pending_ask_user_widget = None
        self.focus_chat_input_after_refresh()

    async def cancel_pending_ask_user(self, *, context: str = "cleanup") -> None:
        """Cancel and remove any mounted pending `ask_user` prompt."""
        if self.pending_ask_user_widget is not None:
            widget = self.pending_ask_user_widget
            self.pending_ask_user_widget = None
            try:
                widget.action_cancel()
            except Exception:
                pass
            if getattr(widget, "is_mounted", False):
                try:
                    await widget.remove()
                except Exception:
                    pass
            self.focus_chat_input_after_refresh()

    # ── Goal Criteria Review Lifecycle ───────────────────

    async def cancel_pending_goal_review(self, *, context: str = "cleanup") -> None:
        """Cancel and remove any mounted pending goal review prompt."""
        logger.debug("Cancelling pending goal review (context=%s)", context)
        widget = self.pending_goal_review_widget
        future = self.pending_goal_review_future
        self.pending_goal_review_widget = None
        self.pending_goal_review_future = None

        if widget is not None:
            try:
                widget.action_cancel()
            except Exception:
                pass
        if future is not None and not future.done():
            future.cancel()
        if widget is not None and getattr(widget, "is_mounted", False):
            try:
                widget.remove()
            except Exception:
                pass
        self.focus_chat_input_after_refresh()

    def open_goal_review(
        self,
        objective: str,
        rubric: str,
        amendment: bool = False,
    ) -> None:
        """Mount the inline goal review widget into messages for user review of criteria."""
        app = self._app
        try:
            messages = app.query_one("#messages", MessageList)
        except NoMatches:
            logger.warning(
                "Cannot mount goal review menu: #messages container not found"
            )
            return

        if self.pending_goal_review_widget is not None:
            try:
                self.pending_goal_review_widget.remove()
            except Exception:
                pass

        unique_id = f"goal-review-menu-{uuid.uuid4().hex[:8]}"
        logger.debug(
            "Mounting GoalReviewMenu id=%s for objective=%r", unique_id, objective[:40]
        )
        menu = GoalReviewMenu(
            objective,
            rubric,
            amendment=amendment,
            id=unique_id,
        )
        self.pending_goal_review_widget = menu
        app.run_worker(messages.mount_inline_prompt(menu))

    async def on_goal_review_decided(self, event: GoalReviewMenu.Decided) -> None:
        """Handle a goal review decision by processing result and restoring focus."""
        app = self._app
        if self.pending_goal_review_widget is event.widget:
            self.pending_goal_review_widget = None

        if event.widget:
            try:
                event.widget.remove()
            except Exception:
                pass

        result = event.result
        res_type = result.get("type")
        logger.debug("GoalReviewMenu decision received: %s", res_type)

        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        state = get_goal_state(app)
        objective = state.pending_objective or state.objective or ""
        prev_rubric = state.pending_rubric or state.rubric or ""

        if res_type == "accepted":
            asyncio.ensure_future(app._accept_goal_rubric(prev_rubric))
        elif res_type == "edited":
            new_criteria: str = str(result.get("criteria", prev_rubric))
            asyncio.ensure_future(app._accept_goal_rubric(new_criteria))
        elif res_type == "rejected":
            feedback: str = str(result.get("message", ""))
            state.pending_objective = None
            state.pending_rubric = None
            GoalHandler._sync_status_rubric(app, state)

            req = {
                "kind": "create",
                "request_id": str(uuid.uuid4()),
                "objective": objective,
                "feedback": feedback,
                "previous_criteria": prev_rubric,
            }
            asyncio.ensure_future(app._run_goal_criteria_request(req))
        elif res_type == "cancelled":
            state.pending_objective = None
            state.pending_rubric = None
            GoalHandler._sync_status_rubric(app, state)

        self.focus_chat_input_after_refresh()


__all__ = ["PromptController"]
