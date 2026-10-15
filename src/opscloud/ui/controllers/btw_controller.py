"""Ephemeral `/btw` side-question controller extracted from `OpsCloudApp`."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from opscloud.ui.widgets.btw_card import BtwCardPanel, BtwExchange
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)


class BtwController:
    """Manages ephemeral `/btw` side-question state, history, and worker execution."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    def refresh_btw_card(self) -> None:
        """Update the `BtwCardPanel` widget from `app._btw_items` and `app._btw_index`."""
        app = self._app
        with suppress(Exception):
            card = app.query_one("#btw-card-panel", BtwCardPanel)
            card.render_state(app._btw_items, app._btw_index)

    def is_btw_active(self) -> bool:
        """Return True if any `/btw` side question is currently loading."""
        return any(item.loading for item in self._app._btw_items)

    def is_btw_open(self) -> bool:
        """Return True if the `/btw` overlay card is currently visible."""
        app = self._app
        return 0 <= app._btw_index < len(app._btw_items)

    def show_btw_history(self) -> bool:
        """Reopen the most recent `/btw` card if history exists (`ShowBtwHistory` parity)."""
        app = self._app
        if not app._btw_items:
            return False
        app._btw_index = len(app._btw_items) - 1
        app._refresh_btw_card()
        return True

    def dismiss_btw_response(self) -> None:
        """Dismiss the `/btw` card and cancel any in-flight `/btw` generation (`DismissBtwResponse` parity)."""
        app = self._app
        app._btw_cancel_event.set()
        if app._btw_worker is not None and not app._btw_worker.is_finished:
            app._cancel_worker(app._btw_worker)
        app._btw_worker = None
        for item in app._btw_items:
            if item.loading:
                item.loading = False
                if not item.error:
                    item.error = "cancelled"
        app._btw_index = -1
        app._refresh_btw_card()

    def clear_active_btw(self) -> None:
        """Clear all `/btw` history and dismiss any open `/btw` card (`ClearActiveBtw` parity)."""
        app = self._app
        app.dismiss_btw_response()
        app._btw_items.clear()
        app._btw_index = -1
        app._refresh_btw_card()

    def toggle_btw_expanded(self) -> None:
        """Toggle expanded/collapsed state on the currently visible `/btw` card (`ToggleBtwExpanded` parity)."""
        app = self._app
        if 0 <= app._btw_index < len(app._btw_items):
            app._btw_items[app._btw_index].expanded = not app._btw_items[app._btw_index].expanded
            app._refresh_btw_card()

    def navigate_btw(self, delta: int) -> None:
        """Navigate backward/forward through `/btw` history (`NavigateBtw` parity)."""
        app = self._app
        if not app._btw_items or app._btw_index < 0:
            return
        new_idx = max(0, min(len(app._btw_items) - 1, app._btw_index + delta))
        if new_idx != app._btw_index:
            app._btw_index = new_idx
            app._refresh_btw_card()

    def get_btw_copy_status(self) -> tuple[str | None, str | None]:
        """Return `(answer, error_message)` for `/copy btw` (`copyBtwAnswer` parity)."""
        app = self._app
        if not app._btw_items:
            return None, "No /btw response to copy."
        idx = app._btw_index if 0 <= app._btw_index < len(app._btw_items) else len(app._btw_items) - 1
        item = app._btw_items[idx]
        if item.loading:
            return None, "The /btw answer is still being generated."
        if item.error:
            return None, f"The /btw question failed: {item.error}"
        if not item.answer.strip():
            return None, "No /btw response to copy."
        return item.answer, None

    async def send_ephemeral_message(self, question: str) -> None:
        """Start an isolated `/btw` side question in a background worker (`SendSideQuestion` parity).

        Runs concurrently even while `app._agent_running` is True without mutating
        the main conversation transcript or checkpoint.
        """
        from opscloud.middleware.btw import BTW_PROMPT_PREFIX

        app = self._app
        clean_q = question.strip()
        if clean_q.startswith(BTW_PROMPT_PREFIX):
            clean_q = clean_q[len(BTW_PROMPT_PREFIX) :].strip()
        elif clean_q.startswith("[ASIDE — ephemeral side question"):
            lines = [ln for ln in clean_q.splitlines() if not ln.startswith("[ASIDE") and not ln.startswith("[/ASIDE")]
            clean_q = "\n".join(lines).strip()

        if app.is_btw_active():
            app.notify(
                "A /btw question is already in progress. Please wait for it to finish.",
                severity="warning",
                timeout=3,
            )
            return

        exchange = BtwExchange(question=clean_q, answer="", loading=True, error="", expanded=False)
        app._btw_items.append(exchange)
        item_index = len(app._btw_items) - 1
        app._btw_index = item_index
        app._btw_cancel_event = asyncio.Event()
        app._refresh_btw_card()

        app._btw_worker = app.run_worker(
            app._run_btw_task(item_index, clean_q),
            group="btw-worker",
            exclusive=False,
        )

    async def run_btw_task(self, item_index: int, question: str) -> None:
        """Execute the isolated `/btw` side question with a 60-second timeout."""
        from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region
        from opscloud.middleware.btw import BTW_TIMEOUT_SECONDS, format_btw_prompt
        from opscloud.ui.app import _format_error_detail

        app = self._app
        client = app._client or (getattr(app._adapter, "_client", None) if app._adapter else None)
        if client is None:
            if 0 <= item_index < len(app._btw_items):
                app._btw_items[item_index].loading = False
                app._btw_items[item_index].error = "Agent not connected"
                app._refresh_btw_card()
            return

        context: dict[str, Any] = {"_btw_side_question": True}
        if app._model:
            context["model"] = app._model
        active_aws_prof = get_active_aws_profile()
        active_aws_reg = get_active_aws_region()
        if active_aws_prof:
            context["aws_profile"] = active_aws_prof
        if active_aws_reg:
            context["aws_region"] = active_aws_reg

        ui_messages = app.get_thread_messages()
        fragments: list[str] = []

        try:
            async with asyncio.timeout(BTW_TIMEOUT_SECONDS):
                if hasattr(client, "astream_side_question"):
                    async for chunk in client.astream_side_question(
                        question,
                        parent_thread_id=app._agent_thread_id,
                        context=context,
                        messages_override=ui_messages,
                    ):
                        if app._btw_cancel_event.is_set():
                            break
                        if chunk:
                            fragments.append(chunk)
                            if 0 <= item_index < len(app._btw_items):
                                app._btw_items[item_index].answer = "".join(fragments)
                                app._refresh_btw_card()
                elif hasattr(client, "astream"):
                    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

                    temp_tid = f"btw-{uuid.uuid4()}"
                    seed_msgs = [
                        m for m in ui_messages if getattr(m, "type", "") in ("human", "HumanMessage", "ai", "AIMessage")
                    ]
                    side_msg = HumanMessage(
                        content=format_btw_prompt(question),
                        additional_kwargs={"ephemeral": True},
                    )
                    async for raw_event in client.astream(
                        {"messages": [*seed_msgs, side_msg], "_btw_side_question": True},
                        stream_mode=["messages", "updates"],
                        subgraphs=False,
                        config={"configurable": {"thread_id": temp_tid}},
                        context=context,
                    ):
                        if app._btw_cancel_event.is_set():
                            break
                        if not isinstance(raw_event, tuple):
                            continue
                        mode = raw_event[1] if len(raw_event) == 3 else raw_event[0]
                        data = raw_event[2] if len(raw_event) == 3 else raw_event[1]
                        if mode == "messages":
                            msg_obj = data[0] if isinstance(data, tuple) else data
                            if isinstance(msg_obj, (AIMessage, AIMessageChunk)) or getattr(
                                msg_obj, "type", ""
                            ) in ("ai", "AIMessage", "AIMessageChunk"):
                                content = getattr(msg_obj, "content", "")
                                if isinstance(content, str) and content:
                                    fragments.append(content)
                                    if 0 <= item_index < len(app._btw_items):
                                        app._btw_items[item_index].answer = "".join(fragments)
                                        app._refresh_btw_card()

            if 0 <= item_index < len(app._btw_items):
                item = app._btw_items[item_index]
                item.loading = False
                if app._btw_cancel_event.is_set() and not item.error:
                    item.error = "cancelled"
                app._refresh_btw_card()
        except asyncio.CancelledError:
            if 0 <= item_index < len(app._btw_items):
                item = app._btw_items[item_index]
                item.loading = False
                if not item.error:
                    item.error = "cancelled"
                app._refresh_btw_card()
        except TimeoutError:
            if 0 <= item_index < len(app._btw_items):
                item = app._btw_items[item_index]
                item.loading = False
                item.error = "timed out after 60s"
                app._refresh_btw_card()
        except Exception as exc:
            logger.debug("/btw side question failed: %s", exc, exc_info=True)
            if 0 <= item_index < len(app._btw_items):
                item = app._btw_items[item_index]
                item.loading = False
                item.error = _format_error_detail(exc) or str(exc)
                app._refresh_btw_card()
