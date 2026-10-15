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


def test_edit_file_renderer_computes_true_line_diff_with_context():
    """Verify EditFileRenderer diffs old_string vs new_string line-by-line rather than dumping all lines."""
    from opscloud.ui.widgets.tool_renderers import EditFileRenderer
    from opscloud.ui.widgets.tool_widgets import _count_diff_stats

    old_block = "line 1\nline 2\nold_value = 1\nline 4\nline 5"
    new_block = "line 1\nline 2\nnew_value = 2\nline 4\nline 5"
    _widget_cls, data = EditFileRenderer.get_approval_widget(
        {
            "file_path": "config.py",
            "old_string": old_block,
            "new_string": new_block,
            "StartLine": 10,
        }
    )
    diff_lines = data["diff_lines"]
    added, removed = _count_diff_stats(diff_lines, old_block, new_block)
    assert added == 1
    assert removed == 1
    assert " line 1" in diff_lines
    assert "-old_value = 1" in diff_lines
    assert "+new_value = 2" in diff_lines


def test_compute_line_diff_folds_distant_unchanged_lines():
    """Verify compute_line_diff keeps 3 context lines and folds larger gaps into hidden markers."""
    from opscloud.ui.widgets.diff import compute_line_diff

    old_lines = [f"ctx {i}" for i in range(1, 15)]
    new_lines = list(old_lines)
    new_lines[0] = "changed 1"
    new_lines[-1] = "changed 14"

    diff_lines = compute_line_diff(
        "\n".join(old_lines), "\n".join(new_lines), context_lines=3, start_line=1
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    assert added == 2
    assert removed == 2
    assert any("lines hidden @@" in ln for ln in diff_lines)


def test_format_file_diff_header_and_render_diff_content_truncation():
    """Verify header format (<path>  +N -M) and 33-line truncation footer (f for full diff)."""
    from opscloud.ui.widgets.diff import format_file_diff_header, render_diff_content

    diff_lines = ["@@ -1,40 +1,40 @@"] + [f"+added line {i}" for i in range(1, 41)]
    header = format_file_diff_header("src/opscloud/app.py", additions=40, deletions=0)
    assert "src/opscloud/app.py" in header.plain
    assert "+40" in header.plain
    assert "File:" not in header.plain

    preview, remaining = render_diff_content("\n".join(diff_lines), max_lines=33, line_width=80)
    assert remaining == 7
    assert "… 7 more lines (f for full diff)" in preview.plain

    expanded, remaining_exp = render_diff_content("\n".join(diff_lines), max_lines=None, line_width=80)
    assert remaining_exp == 0
    assert "more lines (f for full diff)" not in expanded.plain
    assert "added line 40" in expanded.plain


@pytest.mark.asyncio
async def test_approval_menu_full_diff_toggle_and_auto_approve_hint():
    """Verify pressing 'f' toggles full diff in ApprovalMenu and shift+tab hint is shown."""
    from opscloud.ui.widgets.tool_widgets import EditFileApprovalWidget

    old_str = "\n".join(f"line {i}" for i in range(1, 45))
    new_str = "\n".join(f"modified {i}" for i in range(1, 45))
    action_requests = [
        {
            "name": "edit_file",
            "call_id": "call_diff_1",
            "args": {
                "file_path": "src/service.py",
                "old_string": old_str,
                "new_string": new_str,
            },
        }
    ]
    menu = ApprovalMenu(action_requests)
    app = DummyApprovalMenuApp(menu)

    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        edit_widget = menu.query_one(EditFileApprovalWidget)
        assert edit_widget.has_expandable_diff is True
        assert edit_widget._full_diff is False
        assert "f full diff" in str(menu.border_subtitle)

        # Toggle full diff via action_toggle_full_diff ('f' key)
        menu.action_toggle_full_diff()
        await pilot.pause()
        assert edit_widget._full_diff is True

        # Toggle back to compact preview
        menu.action_toggle_full_diff()
        await pilot.pause()
        assert edit_widget._full_diff is False


def test_tool_call_message_renders_diff_stats_and_diff_body_in_transcript():
    """Verify ToolCallMessage header shows +N -M and body renders diff lines for edit_file."""
    from opscloud.ui.widgets.messages import ToolCallMessage

    msg = ToolCallMessage(
        "edit_file",
        {
            "file_path": "src/main.py",
            "old_string": "x = 1\ny = 2\n",
            "new_string": "x = 1\ny = 3\nz = 4\n",
        },
    )
    msg._status = "success"
    header = msg._header_content()
    assert "Edit" in header.plain
    assert "src/main.py" in header.plain
    assert "+2" in header.plain
    assert "-1" in header.plain

    formatted = msg._format_output("Successfully replaced 1 instance", is_preview=True)
    assert "- y = 2" in formatted.content.plain
    assert "+ y = 3" in formatted.content.plain
    assert "+ z = 4" in formatted.content.plain


@pytest.mark.asyncio
async def test_write_and_edit_tools_collapse_under_forward_arrow_summary():
    """Verify Write and Edit tools fold under a collapsible forward-arrow summary like Read."""
    from opscloud.ui.widgets.messages import (
        MessageList,
        ToolCallMessage,
        ToolGroupSummary,
    )

    class DummyMessagesApp(App[None]):
        def compose(self) -> ComposeResult:
            yield MessageList(id="messages")

    app = DummyMessagesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        messages = app.query_one("#messages", MessageList)

        messages.add_tool_call(
            "write_file",
            "call_write_1",
            {"file_path": "report.md", "content": "# Audit Report\nLine 2\n"},
            live=True,
        )
        # Simulate interrupt boundary closing active group while tool is still pending
        await messages.regroup_completed_tools()
        # Simulate tool completion after approval
        messages.update_tool_result("call_write_1", "Wrote 2 lines to report.md", success=True)
        await messages.regroup_completed_tools()
        await pilot.pause()

        summaries = list(messages.query(ToolGroupSummary))
        tool_rows = list(messages.query(ToolCallMessage))
        assert len(summaries) == 1
        assert len(tool_rows) == 1

        summary = summaries[0]
        tool_row = tool_rows[0]

        # By default after completion, the summary shows the forward arrow (▸) and hides the tool row
        assert summary._collapsed is True
        assert tool_row.display is False
        assert "▸ Wrote 1 file" in str(summary.content)

        # Clicking/toggling the forward arrow expands the group to show the tool and its diff
        summary.toggle()
        await pilot.pause()
        assert summary._collapsed is False
        assert tool_row.display is True
        assert "▾ Wrote 1 file" in str(summary.content)

        # Toggling again collapses the tool back behind the forward arrow
        summary.toggle()
        await pilot.pause()
        assert summary._collapsed is True
        assert tool_row.display is False


