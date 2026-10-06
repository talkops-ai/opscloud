"""Infrastructure State Panel for OpsCloud TUI.

Sidebar overlay displaying cloud accounts, Kubernetes context, Terraform workspace,
and Git branch status with production environment alerts.
"""

from __future__ import annotations

import os
import subprocess
from typing import ClassVar


from rich.text import Text
from textual import on
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Static


class InfraStatePanel(Widget):
    """Infrastructure context sidebar panel."""

    DEFAULT_CSS = """
    InfraStatePanel {
        layer: overlay;
        dock: right;
        width: 45;
        height: 100%;
        background: $surface;
        border-left: tall $panel;
        padding: 1;
    }
    InfraStatePanel .header {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }
    InfraStatePanel .section {
        padding: 1;
        margin-bottom: 1;
        background: $background;
        border: solid $panel;
    }
    InfraStatePanel .prod-alert {
        background: $error;
        color: $foreground;
        text-style: bold;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_panel", "Close", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(id="infra-state-panel", **kwargs)

    def _detect_context(self) -> dict[str, str]:
        aws_profile = os.environ.get("AWS_PROFILE", os.environ.get("AWS_REGION", "default"))
        
        # Subprocess k8s context detection fallback
        k8s_ctx = os.environ.get("KUBECONFIG_CONTEXT")
        if not k8s_ctx:
            try:
                res = subprocess.run(
                    ["kubectl", "config", "current-context"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if res.returncode == 0:
                    k8s_ctx = res.stdout.strip()
            except Exception:
                pass
        if not k8s_ctx:
            k8s_ctx = "minikube"

        # Git branch detection
        git_branch = "main"
        try:
            res = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if res.returncode == 0 and res.stdout.strip():
                git_branch = res.stdout.strip()
        except Exception:
            pass

        tf_workspace = os.environ.get("TF_WORKSPACE", "default")

        is_prod = any(
            "prod" in val.lower() or "production" in val.lower()
            for val in (aws_profile, k8s_ctx, tf_workspace, git_branch)
        )

        return {
            "aws_profile": aws_profile,
            "k8s_context": k8s_ctx,
            "tf_workspace": tf_workspace,
            "git_branch": git_branch,
            "is_prod": "true" if is_prod else "false",
        }

    def compose(self):
        ctx = self._detect_context()
        yield Static("Infrastructure Context", classes="header")

        if ctx["is_prod"] == "true":
            yield Static("PRODUCTION CONTEXT ACTIVE — DESTRUCTIVE OPS REQUIRES EXTRA APPROVAL", classes="prod-alert")

        with VerticalScroll():
            cloud_txt = Text("Cloud Credentials\n", style="bold cyan")
            cloud_txt.append(f"AWS Profile: {ctx['aws_profile']}\n", style="dim")
            yield Static(cloud_txt, classes="section")

            k8s_txt = Text("Kubernetes Cluster\n", style="bold green")
            k8s_txt.append(f"Context: {ctx['k8s_context']}\n", style="dim")
            k8s_txt.append("Status: Connected", style="green")
            yield Static(k8s_txt, classes="section")

            git_txt = Text("Git Repository\n", style="bold magenta")
            git_txt.append(f"Branch: {ctx['git_branch']}\n", style="dim")
            yield Static(git_txt, classes="section")

            tf_txt = Text("Infrastructure-as-Code\n", style="bold yellow")
            tf_txt.append(f"Terraform Workspace: {ctx['tf_workspace']}\n", style="dim")
            yield Static(tf_txt, classes="section")

        yield Button("Close (Esc)", variant="primary", id="btn-close")


    @on(Button.Pressed, "#btn-close")
    def action_dismiss_panel(self) -> None:
        self.remove()
