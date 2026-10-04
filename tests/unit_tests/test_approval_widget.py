"""Unit tests for ApprovalMenu and Smart mode HITL approval options."""

import asyncio
import pytest
from opscloud.approval_mode import ApprovalMode
from opscloud.ui.widgets.approval import ApprovalMenu
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
