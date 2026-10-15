"""Unit tests for SkillsViewerScreen and SkillDetailScreen modal UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.theme import register_app_themes
from opscloud.ui.widgets.modal_base import ModalSearchInput
from opscloud.ui.widgets.skills_viewer import SkillDetailScreen, SkillsViewerScreen


class _ThemedSkillsApp(App):
    """Test app with theme support for modal dialogs."""

    def __init__(self, screen_instance: Any) -> None:
        super().__init__()
        self._test_screen = screen_instance
        register_app_themes(self)
        self.theme = "opscloud-dark"

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def _mock_skills() -> list[dict[str, Any]]:
    return [
        {
            "name": "aws-cost-analysis",
            "description": "Analyze AWS billing data, cost anomalies, and underutilized resources.",
            "source": "built-in",
            "path": "/mock/skills/aws-cost-analysis/SKILL.md",
            "tools": ["get_cost_and_usage", "get_anomalies"],
            "tags": ["aws", "cost", "finops"],
        },
        {
            "name": "k8s-pod-troubleshoot",
            "description": "Inspect failing Kubernetes pods, events, and container logs.",
            "source": "plugin",
            "path": "/mock/plugins/k8s/skills/k8s-pod-troubleshoot/SKILL.md",
            "tools": ["kubectl_get_pods", "kubectl_describe", "kubectl_logs"],
            "tags": ["k8s", "debug"],
        },
        {
            "name": "custom-project-skill",
            "description": "Internal automation scripts for project build.",
            "source": "project",
            "path": "/mock/project/.opscloud/skills/custom-project-skill/SKILL.md",
            "tools": [],
            "tags": ["project"],
        },
    ]


@pytest.mark.asyncio
async def test_skills_viewer_modal_mount_and_chrome():
    """Verify SkillsViewerScreen mounts with title counter, keycap subtitle, and two-pane layout."""
    screen = SkillsViewerScreen(skills=_mock_skills())
    app = _ThemedSkillsApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Check dialog title and subtitle
        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "Skills" in str(screen.dialog.border_title)
        assert "3 available" in str(screen.dialog.border_title)

        assert screen.dialog.border_subtitle is not None
        sub_text = str(screen.dialog.border_subtitle)
        assert "navigate" in sub_text
        assert "view SKILL.md" in sub_text
        assert "search" in sub_text
        assert "close" in sub_text

        # Check options count
        opt_list = screen.query_one("#skills-options", OptionList)
        assert opt_list.option_count == 3


@pytest.mark.asyncio
async def test_skills_viewer_filter_search():
    """Verify search input filters the options list and updates title counter."""
    screen = SkillsViewerScreen(skills=_mock_skills())
    app = _ThemedSkillsApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Filter by "k8s"
        search = screen.query_one("#skills-filter", ModalSearchInput)
        search.value = "k8s"
        await pilot.pause()

        assert len(screen._filtered_skills) == 1
        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "1 matches" in str(screen.dialog.border_title)

        opt_list = screen.query_one("#skills-options", OptionList)
        assert opt_list.option_count == 1

        # Clear filter
        search.value = ""
        await pilot.pause()
        assert len(screen._filtered_skills) == 3
        assert "3 available" in str(screen.dialog.border_title)
        assert opt_list.option_count == 3


@pytest.mark.asyncio
async def test_skills_viewer_selection_updates_detail_panel():
    """Verify highlighting an option refreshes the detail pane."""
    screen = SkillsViewerScreen(skills=_mock_skills())
    app = _ThemedSkillsApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Highlight second item (k8s-pod-troubleshoot)
        opt_list = screen.query_one("#skills-options", OptionList)
        opt_list.highlighted = 1
        await pilot.pause()

        detail_container = screen.query_one("#skills-detail-container")
        detail_text = "".join(str(w.render()) for w in detail_container.children)
        assert "k8s-pod-troubleshoot" in detail_text
        assert "PLUGIN" in detail_text
        assert "3 tools" in detail_text


@pytest.mark.asyncio
async def test_skill_detail_screen_modal(tmp_path: Path):
    """Verify SkillDetailScreen mounts cleanly with metadata and content."""
    mock_file = tmp_path / "SKILL.md"
    mock_file.write_text("# My Skill Instructions\nDo something useful.", encoding="utf-8")

    meta = {
        "name": "my-skill",
        "source": "project",
        "path": str(mock_file),
        "tools": ["tool_a", "tool_b"],
    }
    screen = SkillDetailScreen(
        skill_name="my-skill",
        content=mock_file.read_text(encoding="utf-8"),
        skill_meta=meta,
    )
    app = _ThemedSkillsApp(screen)

    async with app.run_test(size=(100, 25)) as pilot:
        await pilot.pause()

        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "SKILL.md" in str(screen.dialog.border_title)
        assert "my-skill" in str(screen.dialog.border_title)

        assert screen.dialog.border_subtitle is not None
        assert "close" in str(screen.dialog.border_subtitle)


@pytest.mark.asyncio
async def test_skills_viewer_namespaced_plugin_skill_formatting():
    """Verify plugin skill names are cleanly parsed into short_name and plugin badges."""
    skills = [
        {
            "name": "aws-compute@talkops-devops-plugins:setting-up-ec2-instance-profiles",
            "description": "Configures EC2 instances to securely call AWS services.",
            "source": "plugin",
            "path": "/mock/plugins/aws-compute/skills/setting-up-ec2-instance-profiles/SKILL.md",
            "tools": [],
            "tags": ["aws"],
        },
        {
            "name": "aws-compute@talkops-devops-plugins:ec2-runner",
            "description": "Quickly runs EC2 instances.",
            "source": "plugin",
            "path": "/mock/plugins/aws-compute/skills/ec2-runner/SKILL.md",
            "tools": [],
            "tags": ["aws"],
        },
    ]
    screen = SkillsViewerScreen(skills=skills)
    app = _ThemedSkillsApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        opt_list = screen.query_one("#skills-options", OptionList)
        assert opt_list.option_count == 2

        # 1. Long skill name: displays clean short_name and [PLUGIN] badge without wrapping
        opt0 = opt_list.get_option_at_index(0)
        plain0 = getattr(opt0.prompt, "plain", None) or str(opt0.prompt)
        assert "setting-up-ec2-instance-profiles" in plain0
        assert "aws-compute@talkops-devops-plugins:" not in plain0
        assert "PLUGIN" in plain0
        assert opt0.id == "skill:aws-compute@talkops-devops-plugins:setting-up-ec2-instance-profiles"

        # 2. Compact skill name: displays plugin identifier as badge [aws-compute]
        opt1 = opt_list.get_option_at_index(1)
        plain1 = getattr(opt1.prompt, "plain", None) or str(opt1.prompt)
        assert "ec2-runner" in plain1
        assert "aws-compute" in plain1

        # 3. Detail pane shows short name in title and full ID under Identifier
        detail_container = screen.query_one("#skills-detail-container")
        detail_text = "".join(str(w.render()) for w in detail_container.children)
        assert "setting-up-ec2-instance-profiles" in detail_text
        assert "aws-compute@talkops-devops-plugins:setting-up-ec2-instance-profiles" in detail_text
        assert "PLUGIN" in detail_text

