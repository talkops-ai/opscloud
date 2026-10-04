"""DevOps-specific terminal output renderers for Terraform, Helm, Kubectl, and Ansible."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static


class TerraformPlanRenderer:
    """Render terraform plan output with add/change/destroy highlighting."""

    def render(self, output: str) -> Text:
        lines = output.splitlines()
        text = Text()

        for line in lines:
            line_stripped = line.strip()

            if "Plan:" in line:
                line_text = Text(line, style="bold cyan")
            elif line_stripped.startswith("+"):
                line_text = Text(line, style="bold green")
            elif line_stripped.startswith("-"):
                line_text = Text(line, style="bold red")
            elif line_stripped.startswith("~") or line_stripped.startswith("!~"):
                line_text = Text(line, style="bold yellow")
            elif "to add," in line and "to change," in line and "to destroy" in line:
                line_text = Text(line, style="bold magenta")
            else:
                line_text = Text(line)

            text.append(line_text)
            text.append("\n")

        return text

    def render_json(self, plan_data: dict[str, Any]) -> Text:
        """Render structured terraform show -json plan data."""
        text = Text()
        resource_changes = plan_data.get("resource_changes", [])
        add_cnt = sum(1 for r in resource_changes if "create" in r.get("change", {}).get("actions", []))
        change_cnt = sum(1 for r in resource_changes if "update" in r.get("change", {}).get("actions", []))
        delete_cnt = sum(1 for r in resource_changes if "delete" in r.get("change", {}).get("actions", []))

        text.append(f"📊 Terraform Plan Summary: +{add_cnt} add, ~{change_cnt} change, -{delete_cnt} destroy\n\n", style="bold magenta")
        for r in resource_changes:
            address = r.get("address", "resource")
            actions = r.get("change", {}).get("actions", [])
            if "create" in actions:
                text.append(f"  + {address} (create)\n", style="bold green")
            elif "delete" in actions:
                text.append(f"  - {address} (destroy)\n", style="bold red")
            elif "update" in actions:
                text.append(f"  ~ {address} (update)\n", style="bold yellow")
        return text


class TerraformPlanWidget(Widget):
    """Widget container displaying formatted Terraform plan outputs."""

    DEFAULT_CSS = """
    TerraformPlanWidget {
        padding: 1;
        margin: 1 0;
        background: $surface;
        border: solid $primary;
    }
    """

    def __init__(self, plan_output: str, is_json: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._plan_output = plan_output
        self._is_json = is_json

    def compose(self):
        renderer = TerraformPlanRenderer()
        if self._is_json:
            try:
                data = json.loads(self._plan_output)
                rendered = renderer.render_json(data)
            except Exception:
                rendered = renderer.render(self._plan_output)
        else:
            rendered = renderer.render(self._plan_output)

        yield Static("🏗️ Terraform Plan Output", classes="title")
        yield Static(rendered)


class HelmDiffRenderer:
    """Render helm diff with chart value change highlights."""

    def render(self, output: str) -> Text:
        lines = output.splitlines()
        text = Text()

        for line in lines:
            line_stripped = line.strip()

            if line_stripped.startswith("+") and not line_stripped.startswith("+++"):
                line_text = Text(line, style="bold green")
            elif line_stripped.startswith("-") and not line_stripped.startswith("---"):
                line_text = Text(line, style="bold red")
            elif line_stripped.startswith("@@"):
                line_text = Text(line, style="cyan")
            elif line_stripped.startswith("+++") or line_stripped.startswith("---"):
                line_text = Text(line, style="bold")
            else:
                line_text = Text(line)

            text.append(line_text)
            text.append("\n")

        return text


class KubectlRenderer:
    """Render kubectl output with resource status coloring."""

    def render(self, output: str) -> Text:
        lines = output.splitlines()
        text = Text()

        green_pattern = re.compile(r"\b(Running|Ready|Completed|Succeeded|Active)\b", re.IGNORECASE)
        yellow_pattern = re.compile(r"\b(Pending|ContainerCreating|Terminating|ScalingUp|ScalingDown)\b", re.IGNORECASE)
        red_pattern = re.compile(r"\b(CrashLoopBackOff|Error|OOMKilled|ImagePullBackOff|ErrImagePull|Failed|Evicted)\b", re.IGNORECASE)

        for line in lines:
            line_text = Text(line)

            for match in green_pattern.finditer(line):
                line_text.stylize("bold green", match.start(), match.end())

            for match in yellow_pattern.finditer(line):
                line_text.stylize("bold yellow", match.start(), match.end())

            for match in red_pattern.finditer(line):
                line_text.stylize("bold red", match.start(), match.end())

            text.append(line_text)
            text.append("\n")

        return text


class AnsiblePlaybookRenderer:
    """Render ansible playbook task execution output."""

    def render(self, output: str) -> Text:
        lines = output.splitlines()
        text = Text()

        for line in lines:
            if "TASK [" in line or "PLAY [" in line:
                line_text = Text(line, style="bold cyan")
            elif "ok:" in line:
                line_text = Text(line, style="green")
            elif "changed:" in line:
                line_text = Text(line, style="yellow")
            elif "failed:" in line or "fatal:" in line:
                line_text = Text(line, style="bold red")
            elif "PLAY RECAP" in line:
                line_text = Text(line, style="bold magenta")
            else:
                line_text = Text(line)

            text.append(line_text)
            text.append("\n")

        return text


__all__ = [
    "AnsiblePlaybookRenderer",
    "HelmDiffRenderer",
    "KubectlRenderer",
    "TerraformPlanRenderer",
    "TerraformPlanWidget",
]
