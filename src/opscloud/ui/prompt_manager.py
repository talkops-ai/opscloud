import asyncio
from opscloud.utils.logger import get_logger
import uuid
from typing import TYPE_CHECKING, Callable

from textual.containers import Container, VerticalScroll
from textual.widget import Widget

from opscloud.ui.widgets._ask_user_types import Question
from opscloud.ui.widgets.ask_user import AskUserMenu

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)

class PromptManager:
    """Manages inline prompts, extracting UI complexity from app.py."""

    def __init__(self, app: "OpsCloudApp"):
        self._app = app
        self._pending_ask_user_widget: AskUserMenu | None = None

    async def _mount_inline_prompt(
        self,
        widget: Widget,
        *,
        focus: Callable[[], None],
    ) -> None:
        """Mount, scroll, and focus an inline prompt safely via app thread."""
        messages = self._app.query_one("#messages", VerticalScroll)
        
        await messages.mount(widget)
        try:
            messages.scroll_end(animate=False)
            focus()
        except Exception:
            pass

    async def _remove_inline_prompt_widget(self, widget: Widget) -> None:
        """Remove a widget safely."""
        try:
            widget.remove()
        except Exception:
            pass
            
        def _refocus_chat_input():
            try:
                chat_input = getattr(self._app, "_chat_input", None)
                if chat_input:
                    chat_input.focus()
            except Exception:
                pass
                
        self._app.call_after_refresh(_refocus_chat_input)

    async def present_ask_user(self, questions: list[Question]) -> list[str] | None:
        """Mount AskUserMenu and await its result."""
        if self._pending_ask_user_widget:
            # Cancel any existing ask user widget
            self._pending_ask_user_widget.action_cancel()
            await self._remove_inline_prompt_widget(self._pending_ask_user_widget)
            self._pending_ask_user_widget = None
            
        unique_id = f"ask-user-menu-{uuid.uuid4().hex[:8]}"
        menu = AskUserMenu(questions, id=unique_id)
        
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()
        menu.set_future(result_future)

        self._pending_ask_user_widget = menu

        try:
            await self._mount_inline_prompt(menu, focus=menu.focus_active)
        except Exception as e:
            self._app.notify(f"ask_user mount exception: {e!r}", severity="error")
            self._pending_ask_user_widget = None
            return None
            
        try:
            # Wait for the user to answer or cancel
            result = await result_future
        except asyncio.CancelledError:
            self._app.notify("ask_user future was cancelled", severity="error")
            return None
        except Exception as e:
            self._app.notify(f"ask_user future exception: {e!r}", severity="error")
            return None
        finally:
            self._pending_ask_user_widget = None
            await self._remove_inline_prompt_widget(menu)
                
        if result.get("type") == "answered":
            return result.get("answers")
        return None
