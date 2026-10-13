import asyncio
import os
import pytest
from textual.app import App, ComposeResult
from opscloud.approval_mode import ApprovalMode
from opscloud.ui.widgets.approval import (
    ApprovalDecided,
    ApprovalMenu,
    ApprovalModalScreen,
    assess_tool_risk,
    get_target_context_pill,
    highlight_command,
)
from opscloud.ui.app import OpsCloudApp


def test_approval_menu_build_options_shows_smart_mode():
    """Verify that ApprovalMenu presents 3 options: Approve, Enable Smart mode, Reject."""
    action_requests = [
        {"name": "execute", "call_id": "call_1", "args": {"command": "terraform apply"}}
    ]
    menu = ApprovalMenu(action_requests)
    options = menu._build_options()

    assert len(options) == 3
    assert options[0] == ("Approve (y)", "approve")
    assert options[1] == ("Enable Smart mode for this thread (s)", "smart_approve_all")
    assert options[2] == ("Reject (n)", "reject")


def test_approval_menu_build_options_fallback_shows_manual():
    """Verify that in fallback mode, option 2 is Switch to Manual."""
    action_requests = [
        {
            "name": "execute",
            "call_id": "call_1",
            "args": {"command": "terraform apply"},
            "description": "Auto human fallback - policy denied",
        }
    ]
    menu = ApprovalMenu(action_requests)
    options = menu._build_options()

    assert len(options) == 3
    assert options[0] == ("Approve (y)", "approve")
    assert options[1] == ("Switch to Manual (s)", "switch_manual")
    assert options[2] == ("Reject (n)", "reject")


def test_approval_menu_help_text_shows_smart_key():
    """Verify help text displays y/s/n quick keys."""
    action_requests = [
        {"name": "execute", "call_id": "call_1", "args": {"command": "ls -la"}}
    ]
    menu = ApprovalMenu(action_requests)
    help_text = menu._compose_help_text()
    assert "y/s/n" in help_text


def test_approval_menu_action_select_smart_resolves_future():
    """Verify action_select_smart() selects smart_approve_all and marks approved."""
    action_requests = [
        {"name": "execute", "call_id": "call_42", "args": {"command": "echo test"}}
    ]
    menu = ApprovalMenu(action_requests)

    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    menu.set_future(fut)

    # Calling action_select_smart
    menu.action_select_smart()

    assert fut.done()
    res = fut.result()
    assert res == {"type": "smart_approve_all"}


def test_approval_menu_action_select_auto_aliases_smart():
    """Verify legacy action_select_auto() also selects smart_approve_all."""
    action_requests = [
        {"name": "execute", "call_id": "call_43", "args": {"command": "echo test"}}
    ]
    menu = ApprovalMenu(action_requests)

    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    menu.set_future(fut)

    menu.action_select_auto()

    assert fut.done()
    res = fut.result()
    assert res == {"type": "smart_approve_all"}


@pytest.mark.asyncio
async def test_app_on_approval_menu_decided_activates_smart_mode():
    """Verify that when ApprovalMenu decides smart_approve_all, app switches to SMART mode."""
    app = OpsCloudApp(defer_server_start=True, approval_mode="manual")
    assert app._approval_mode == ApprovalMode.MANUAL

    decided_event = ApprovalMenu.Decided(
        decision={"type": "smart_approve_all"},
        approved=True,
        tool_name="execute",
        call_id="call_99",
    )

    await app._on_approval_menu_decided(decided_event)
    assert app._approval_mode == ApprovalMode.SMART


@pytest.mark.asyncio
async def test_app_on_smart_approve_enabled_activates_smart_mode():
    """Verify that _on_smart_approve_enabled switches app to SMART mode."""
    app = OpsCloudApp(defer_server_start=True, approval_mode="manual")
    assert app._approval_mode == ApprovalMode.MANUAL

    res = await app._on_smart_approve_enabled()
    assert res is True
    assert app._approval_mode == ApprovalMode.SMART


def test_assess_tool_risk_destructive_keywords():
    assert assess_tool_risk("execute", {"command": "terraform destroy -auto-approve"}, is_prod=False) == "high"
    assert assess_tool_risk("execute", {"command": "rm -rf /tmp/data"}, is_prod=False) == "high"
    assert assess_tool_risk("execute", {"command": "kubectl delete pod my-pod"}, is_prod=False) == "high"
    assert assess_tool_risk("execute", {"command": "ls -la"}, is_prod=False) == "low"
    assert assess_tool_risk("execute", {"command": "git status"}, is_prod=False) == "low"
    assert assess_tool_risk("replace_file_content", {"TargetFile": "app.py"}, is_prod=False) == "medium"


def test_assess_tool_risk_prod_detection():
    # Explicit is_prod
    assert assess_tool_risk("execute", {"command": "ls -la"}, is_prod=True) == "prod"
    # Auto-detected prod via aws_profile
    assert assess_tool_risk("execute", {"command": "ls -la"}, aws_profile="client-prod") == "prod"
    # Auto-detected prod via k8s_context
    assert assess_tool_risk("execute", {"command": "ls -la"}, k8s_context="prod-cluster") == "prod"
    # Non-prod context
    assert assess_tool_risk("execute", {"command": "ls -la"}, aws_profile="dev", k8s_context="dev", tf_workspace="default") == "low"


def test_highlight_command_and_prompt_prefix():
    content = highlight_command("rm -rf /var/log")
    rendered = str(content)
    assert "$" in rendered
    assert "rm -rf" in rendered


def test_get_target_context_pill():
    pill = get_target_context_pill(aws_profile="client-prod", aws_region="us-east-1")
    assert "aws: client-prod" in pill
    assert "us-east-1" in pill


class DummyApprovalMenuApp(App[None]):
    def __init__(self, menu: ApprovalMenu) -> None:
        super().__init__()
        self._menu = menu

    def compose(self) -> ComposeResult:
        yield self._menu


@pytest.mark.asyncio
async def test_approval_menu_mount_chrome_and_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "client-prod")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    action_requests = [
        {"name": "execute", "call_id": "call_1", "args": {"command": "terraform apply"}}
    ]
    menu = ApprovalMenu(action_requests)
    app = DummyApprovalMenuApp(menu)

    async with app.run_test() as pilot:
        # Check border title is clean and generic: " Tool Approval "
        assert menu.border_title is not None
        title_str = str(menu.border_title)
        assert "Tool Approval" in title_str
        assert "PROD OPERATION" not in title_str
        assert "client-prod" not in title_str

        # Check options formatting uses (y), (s), (n)
        assert menu._format_option_display("Approve (y)") == "(y) Approve once"
        assert (
            menu._format_option_display("Enable Smart mode for this thread (s)")
            == "(s) Enable Smart mode for this session"
        )
        assert menu._format_option_display("Reject (n)") == "(n) Reject"

        # Check border subtitle has key hints
        assert menu.border_subtitle is not None
        subtitle_str = str(menu.border_subtitle)
        assert "y approve" in subtitle_str
        assert "s smart" in subtitle_str
        assert "n reject" in subtitle_str

        # Check options styling: selected index 0 has class
        assert "approval-option-selected" in menu._option_widgets[0].classes

        # Navigate down
        menu.action_move_down()
        await pilot.pause()
        assert menu._selected == 1
        assert "approval-option-selected" in menu._option_widgets[1].classes

        # Quick approve
        menu.action_select_approve()
        await pilot.pause()
        assert not menu.display


class DummyApprovalModalApp(App[None]):
    def __init__(self, screen_instance: ApprovalModalScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_approval_modal_screen_actions() -> None:
    modal = ApprovalModalScreen(
        tool_name="execute",
        call_id="call-42",
        args={"command": "terraform destroy -auto-approve"},
    )
    app = DummyApprovalModalApp(modal)

    async with app.run_test() as pilot:
        assert modal.dialog is not None
        assert modal.dialog.border_title is not None
        assert "Tool Execution Approval" in str(modal.dialog.border_title)
        assert modal.dialog.border_subtitle is not None

        # Test approve action
        modal.action_approve()
        await pilot.pause()
        assert len(app._screen_stack) == 1


def test_smart_mode_option_display_no_strikethrough():
    """Verify that formatting (s) Enable Smart mode does not trigger Rich strikethrough."""
    from rich.markup import render
    from opscloud.ui.widgets.approval import ApprovalMenu

    menu = ApprovalMenu([{"name": "execute", "args": {"command": "ls"}}])
    opt_str = menu._format_option_display("Enable Smart mode for this thread (s)")
    assert opt_str.startswith("(s)")
    rendered = render(opt_str)
    # Ensure there is no 'strike' span in rendered text
    assert all(span.style != "strike" for span in rendered.spans)


def test_write_file_renderer_generates_diff_for_existing_file(tmp_path):
    """Verify WriteFileRenderer computes diff when file exists on disk."""
    from opscloud.ui.widgets.tool_renderers import WriteFileRenderer
    from opscloud.ui.widgets.tool_widgets import EditFileApprovalWidget

    test_file = tmp_path / "sample.txt"
    test_file.write_text("line 1\nold line 2\nline 3\n")

    tool_args = {
        "file_path": str(test_file),
        "content": "line 1\nnew line 2\nline 3\n",
    }
    widget_cls, data = WriteFileRenderer.get_approval_widget(tool_args)
    assert widget_cls == EditFileApprovalWidget
    diff_lines = data.get("diff_lines", [])
    assert any("-old line 2" in ln for ln in diff_lines)
    assert any("+new line 2" in ln for ln in diff_lines)


def test_edit_file_renderer_with_start_line():
    """Verify EditFileRenderer uses StartLine to generate hunk with accurate line numbers."""
    from opscloud.ui.widgets.tool_renderers import EditFileRenderer

    tool_args = {
        "file_path": "app.py",
        "TargetContent": "print('hello')",
        "ReplacementContent": "print('world')",
        "StartLine": 55,
    }
    widget_cls, data = EditFileRenderer.get_approval_widget(tool_args)
    diff_lines = data.get("diff_lines", [])
    assert diff_lines
    assert "@@ -55,1 +55,1 @@" in diff_lines[0]
    assert "-print('hello')" in diff_lines[1]
    assert "+print('world')" in diff_lines[2]


def test_multi_replace_file_content_renderer():
    """Verify multi_replace_file_content generates diffs for each replacement chunk."""
    from opscloud.ui.widgets.tool_renderers import get_renderer

    tool_args = {
        "TargetFile": "main.py",
        "ReplacementChunks": [
            {
                "StartLine": 10,
                "EndLine": 10,
                "TargetContent": "a = 1",
                "ReplacementContent": "a = 2",
            },
            {
                "StartLine": 25,
                "EndLine": 25,
                "TargetContent": "b = 10",
                "ReplacementContent": "b = 20",
            },
        ],
    }
    renderer = get_renderer("multi_replace_file_content")
    widget_cls, data = renderer.get_approval_widget(tool_args)
    diff_lines = data.get("diff_lines", [])
    assert any("@@ -10," in ln for ln in diff_lines)
    assert any("@@ -25," in ln for ln in diff_lines)
