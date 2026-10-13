"""Unit tests for ThreadSelectorScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from opscloud.ui.widgets.thread_selector import (
    CustomOptionList,
    DeleteThreadConfirmScreen,
    SearchInput,
    ThreadSelectorScreen,
    _format_relative_time,
)


class DummyThreadApp(App):
    """Minimal textual app to mount and test ThreadSelectorScreen."""

    def __init__(self, screen_instance: ThreadSelectorScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def test_format_relative_time() -> None:
    assert _format_relative_time(None) == ""
    assert _format_relative_time("") == ""
    # Test recent seconds
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    assert _format_relative_time(now_iso) == "just now"


@pytest.mark.asyncio
async def test_thread_selector_screen_mount_and_options() -> None:
    threads = [
        {
            "thread_id": "thread-12345678-abcd",
            "message_count": 5,
            "initial_prompt": "Investigate ECS CPU spike",
            "created_at": "2026-10-12T05:00:00Z",
            "cost": 0.0425,
        },
        {
            "thread_id": "thread-87654321-efgh",
            "message_count": 2,
            "initial_prompt": "Deploy redis cluster",
            "created_at": "2026-10-12T07:00:00Z",
        },
    ]

    screen = ThreadSelectorScreen(threads=threads, current_thread_id="thread-12345678-abcd")
    app = DummyThreadApp(screen)

    async with app.run_test() as pilot:
        # SearchInput is mounted and focused
        search = screen.query_one(SearchInput)
        assert search.has_focus

        # Option list is mounted
        opt_list = screen.query_one(CustomOptionList)
        assert opt_list is not None
        # 1 ("+ Start New Thread") + 2 threads = 3 options
        assert opt_list.option_count == 3

        # Test search filter
        search.value = "redis"
        await pilot.pause()
        assert opt_list.option_count == 2  # New thread + matching thread

        # Clear filter
        search.value = ""
        await pilot.pause()
        assert opt_list.option_count == 3

        # Test down navigation
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 1

        # Test cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_thread_search_editing_keys_do_not_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        search = screen.query_one(SearchInput)
        search.focus()
        await pilot.press("d", "x", "backspace", "d")
        await pilot.pause()

        assert deleted == []
        assert search.value == "dd"


@pytest.mark.asyncio
async def test_thread_row_click_does_not_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    dismiss_result: list[object] = []
    screen.dismiss = lambda result=None: dismiss_result.append(result)  # type: ignore[method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        screen.action_cursor_down()
        await pilot.pause()
        opt_list.action_select()
        await pilot.pause()

        assert deleted == []
        assert dismiss_result == ["thread-12345678-abcd"]


@pytest.mark.asyncio
async def test_thread_ctrl_d_and_confirm_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        # Navigate to the thread option
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 1

        # Press ctrl+d to trigger delete
        await pilot.press("ctrl+d")
        await pilot.pause()

        # Confirmation screen should now be top screen
        confirm_screen = app.screen
        assert isinstance(confirm_screen, DeleteThreadConfirmScreen)
        assert confirm_screen.thread_id == "thread-12345678-abcd"

        # Cancel should not delete
        await pilot.click("#cancel")
        await pilot.pause()
        assert deleted == []

        # Press ctrl+d again and confirm
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, DeleteThreadConfirmScreen)

        await pilot.click("#confirm")
        await pilot.pause()
        assert deleted == ["thread-12345678-abcd"]


@pytest.mark.asyncio
async def test_thread_row_del_click_triggers_confirm_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        screen.action_cursor_down()
        await pilot.pause()

        # Simulate click on [del] zone at the right edge
        opt_list.last_click_x = 95
        opt_list.action_select()
        await pilot.pause()

        # Confirm screen opened, no immediate deletion
        assert isinstance(app.screen, DeleteThreadConfirmScreen)
        assert deleted == []


