"""Unit tests for command output styling, CommandOutputMessage, and Markdown rendering."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from opscloud.commands._base import CommandContext
from opscloud.commands.core.doctor import DiagnosticItem, DiagnosticSection, DoctorHandler
from opscloud.commands.core.help_cmd import HelpHandler
from opscloud.commands.core.mcp import McpHandler
from opscloud.commands.power.tasks import SessionTaskStore, TasksHandler
from opscloud.commands.power.version import VersionHandler
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.ui.widgets.messages import (
    CommandOutputMessage,
    SystemMessage,
    _markdown_to_content,
)


def test_command_output_message_inheritance_and_classes() -> None:
    """CommandOutputMessage must inherit from SystemMessage and carry card classes."""
    msg = CommandOutputMessage("### Test Header\n\nSome command output.", command="/test")
    assert isinstance(msg, SystemMessage)
    assert msg.has_class("-card")
    assert msg.has_class("-command-output")
    assert msg.command == "/test"
    assert msg._is_command_mode() is True


def test_system_message_auto_card_detection() -> None:
    """Multi-line or structured Markdown automatically activates card mode."""
    # Plain single-line
    simple = SystemMessage("Command /foo queued...")
    assert not simple.has_class("-command-output")
    assert simple._build_display_text().startswith("● ")

    # Multi-line
    multiline = SystemMessage("Line 1\nLine 2")
    assert multiline.has_class("-card")
    assert multiline.has_class("-command-output")
    assert not multiline._build_display_text().startswith("● ")

    # Markdown header
    md = SystemMessage("### Result\nAll OK")
    assert md.has_class("-card")
    assert md.has_class("-command-output")


def test_markdown_to_content_command_mode() -> None:
    """_markdown_to_content with is_command=True renders styled content."""
    content = _markdown_to_content(
        "### Header\n- Item 1\n- Item 2\n\n```python\nprint('hi')\n```",
        width=80,
        is_command=True,
    )
    assert content is not None
    assert "Header" in content.plain
    assert "Item 1" in content.plain


@pytest.mark.asyncio
async def test_doctor_output_markdown_structure() -> None:
    """Doctor command must output rich Markdown table without raw ASCII tree codeblocks."""
    handler = DoctorHandler()
    sections = [
        DiagnosticSection(
            title="Diagnostics",
            items=[
                DiagnosticItem("opscloud", "0.1.0"),
                DiagnosticItem("Python", "3.12.11"),
            ],
        ),
        DiagnosticSection(
            title="DevOps Tools",
            items=[
                DiagnosticItem("terraform", "1.6.0"),
                DiagnosticItem("kubectl", "not found", ok=False, tip="Install kubectl"),
            ],
        ),
    ]
    report = handler._render_tree(sections)

    # Must be structured Markdown, NOT inside a code fence, NO emojis
    assert "### OpsCloud Health Diagnostics" in report
    assert "| Diagnostic Category | Status | Details |" in report
    assert "| **Diagnostics** | OK |" in report
    assert "| **DevOps Tools** | CHECK |" in report
    assert "#### Attention Needed" in report
    assert "Install kubectl" in report
    assert "```" not in report  # No raw ASCII codeblock!
    assert "🩺" not in report
    assert "⚙️" not in report
    assert "⚠️" not in report


@pytest.mark.asyncio
async def test_version_output_markdown() -> None:
    """Version command output must be formatted with Markdown headers and bullet points."""
    handler = VersionHandler()
    ctx = CommandContext(app=None, args="")
    res = await handler.execute(ctx)
    assert res.success is True
    assert res.message is not None
    assert "### OpsCloud Version & Runtime" in res.message
    assert "ℹ️" not in res.message
    assert "- **OpsCloud CLI:**" in res.message
    assert "- **Python:**" in res.message


@pytest.mark.asyncio
async def test_help_output_markdown() -> None:
    """Help command output must feature clean Markdown headers and shortcut lists."""
    handler = HelpHandler()
    ctx = CommandContext(app=None, args="")
    res = await handler.execute(ctx)
    assert res.success is True
    assert res.message is not None
    assert "### OpsCloud Commands & Shortcuts" in res.message
    assert "💡" not in res.message
    assert "**Slash Commands:**" in res.message
    assert "**Keyboard Shortcuts:**" in res.message


@pytest.mark.asyncio
async def test_tasks_output_markdown() -> None:
    """Tasks command output must render as a markdown checklist."""
    store = SessionTaskStore()
    t1 = store.add("Review terraform plan")
    t2 = store.add("Apply changes")
    store.done(t1.id)

    handler = TasksHandler()
    ctx = CommandContext(app=None, args="")
    res = handler._list_tasks(store, ctx)
    assert res.success is True
    assert res.message is not None
    assert "### Session Tasks" in res.message
    assert "📋" not in res.message
    assert f"- [x] `#{t1.id}` ~~Review terraform plan~~" in res.message
    assert f"- [ ] `#{t2.id}` Apply changes" in res.message
