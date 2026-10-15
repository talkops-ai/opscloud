"""Approval widget for HITL - using standard Textual patterns."""

from __future__ import annotations

import asyncio
import os
import re
from opscloud.utils.logger import get_logger
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events, on
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, Input, Static, TextArea

from opscloud.ui.widgets.modal_base import OpsModalScreen, escape_markup as escape

if TYPE_CHECKING:
    from textual.app import ComposeResult

import uuid

from opscloud.ui import theme
from opscloud.config.settings import (
    get_glyphs,
    is_ascii_mode,
)
from opscloud.ui.widgets.tool_renderers import get_renderer, render_tool_approval
from opscloud.ui.widgets.diff import compose_diff_lines
from opscloud.security.unicode_security import (
    check_url_safety,
    detect_dangerous_unicode,
    format_warning_detail,
    iter_string_values,
    looks_like_url_key,
    render_with_unicode_markers,
    strip_dangerous_unicode,
    summarize_issues,
)

logger = get_logger(__name__)

_SHELL_COMMAND_TRUNCATE_LENGTH: int = 120
_SHELL_COMMAND_TRUNCATE_LINES: int = 5
_WARNING_PREVIEW_LIMIT: int = 3
_WARNING_TEXT_TRUNCATE_LENGTH: int = 220

_DESTRUCTIVE_PATTERN = re.compile(
    r"\b(rm\s+-rf|rm\s+-r|rm|destroy|drop\s+database|drop\s+table|drop|delete|terminate)\b",
    re.IGNORECASE,
)


def get_target_context_pill(
    *,
    aws_profile: str = "",
    aws_region: str = "",
    k8s_context: str = "",
    tf_workspace: str = "",
) -> str:
    """Build a compact target context pill, e.g. '[aws: client-prod · us-east-1]'."""
    from opscloud.ui.context_detection import current_k8s_context

    p_aws = (
        aws_profile
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_DEFAULT_PROFILE")
        or ""
    )
    p_reg = (
        aws_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ""
    )
    p_k8s = k8s_context or current_k8s_context()
    p_tf = tf_workspace or os.environ.get("TF_WORKSPACE", "")

    parts: list[str] = []
    if p_aws:
        aws_str = f"aws: {p_aws}"
        if p_reg:
            aws_str += f" · {p_reg}"
        parts.append(aws_str)
    if p_k8s:
        parts.append(f"k8s: {p_k8s}")
    if p_tf:
        parts.append(f"tf: {p_tf}")

    if parts:
        return f"[{' | '.join(parts)}]"
    return ""


def highlight_command(command: str) -> Content:
    """Format shell command with '$ ' prefix and highlight destructive verbs in $error bold."""
    lines = command.split("\n")
    formatted_lines: list[str] = []
    for i, line in enumerate(lines):
        prefix = "[dim]$[/dim] " if i == 0 else "  "
        escaped_line = escape(line)
        highlighted = _DESTRUCTIVE_PATTERN.sub(
            lambda m: f"[bold $error]{m.group(0)}[/bold $error]",
            escaped_line,
        )
        formatted_lines.append(f"{prefix}{highlighted}")

    return Content.from_markup("\n".join(formatted_lines))


def _is_command_too_long(command: str) -> bool:
    """Whether a shell command exceeds the display thresholds."""
    if len(command) > _SHELL_COMMAND_TRUNCATE_LENGTH:
        return True
    return command.count("\n") + 1 > _SHELL_COMMAND_TRUNCATE_LINES


def _truncate_command(command: str) -> str:
    """Truncate a shell command for compact display."""
    ellipsis = get_glyphs().ellipsis
    lines = command.split("\n")
    truncated = len(lines) > _SHELL_COMMAND_TRUNCATE_LINES
    if truncated:
        command = "\n".join(lines[:_SHELL_COMMAND_TRUNCATE_LINES])
    if len(command) > _SHELL_COMMAND_TRUNCATE_LENGTH:
        command = command[:_SHELL_COMMAND_TRUNCATE_LENGTH]
        truncated = True
    return command + ellipsis if truncated else command


class ApprovalMenu(Container):
    """Approval menu for Human-In-The-Loop tool execution approvals."""

    can_focus = True
    can_focus_children = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("1", "select_position(0)", "Select first", show=False),
        Binding("2", "select_position(1)", "Select second", show=False),
        Binding("3", "select_position(2)", "Select third", show=False),
        Binding("y", "select_approve", "Approve", show=False),
        Binding("s", "select_smart", "Smart-approve", show=False),
        Binding("a", "select_smart", "Smart-approve (legacy)", show=False),
        Binding("n", "select_reject", "Reject", show=False),
        Binding("escape", "select_reject", "Reject", show=False),
        Binding("e", "toggle_expand", "Expand command", show=False),
        Binding("f", "toggle_full_diff", "Full diff", show=False),
        Binding("shift+tab", "select_smart", "Auto-approve file edits", show=False),
        Binding("tab", "reject_with_reason", "Reject with reason", show=False),
    ]

    DEFAULT_CSS = """
    ApprovalMenu {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        background: $surface;
        border: round $primary;
        border-title-align: left;
        border-subtitle-align: left;
    }
    ApprovalMenu .approval-title {
        display: none;
    }
    ApprovalMenu .approval-header-info {
        height: auto;
        margin: 0 0 1 0;
    }
    ApprovalMenu .approval-meta-line {
        height: auto;
        color: $text;
    }
    ApprovalMenu .approval-command-box {
        height: auto;
        background: $background;
        border: round $panel;
        padding: 0 1;
        margin: 0 0 1 0;
        border-title-align: left;
    }
    ApprovalMenu .approval-command {
        height: auto;
        color: $text;
    }
    ApprovalMenu .tool-info-scroll {
        height: auto;
        max-height: 40;
        overflow-y: auto;
        margin-top: 0;
    }
    ApprovalMenu .tool-info-container {
        height: auto;
    }
    ApprovalMenu .approval-separator {
        height: 1;
        border-top: solid $panel;
        margin: 0;
    }
    ApprovalMenu .approval-options-container {
        height: auto;
        background: $surface;
        padding: 0 0;
        margin: 0 0 1 0;
    }
    ApprovalMenu .approval-option {
        height: 1;
        padding: 0 1;
    }
    ApprovalMenu .approval-option-selected {
        background: $primary 12%;
        color: $text;
        text-style: bold;
    }
    ApprovalMenu .approval-reason-input {
        margin: 1 0 0 0;
        border: round $warning;
        background: $surface;
    }
    ApprovalMenu .approval-help {
        display: none;
    }
    """

    class Decided(Message):
        """Message sent when user makes a decision."""

        def __init__(
            self,
            decision: dict[str, str],
            approved: bool | None = None,
            tool_name: str = "",
            call_id: str = "",
            comment: str = "",
        ) -> None:
            super().__init__()
            self.decision = decision
            # For backwards compatibility with legacy callers
            self.approved = approved if approved is not None else (decision.get("type") in {"approve", "auto_approve_all", "smart_approve_all"})
            self.tool_name = tool_name
            self.call_id = call_id
            self.comment = comment or decision.get("message", "")

    _MINIMAL_TOOLS: ClassVar[frozenset[str]] = frozenset({"execute", "run_command", "bash", "shell"})

    def __init__(
        self,
        action_requests: list[dict[str, Any]] | dict[str, Any] | str,
        call_id_or_assistant: str | None = None,
        args: dict[str, Any] | None = None,
        id: str | None = None,
        *,
        auto_mode_eligible: bool = True,
        risk: str = "low",
        is_prod: bool | None = None,
        **kwargs: Any,
    ) -> None:
        menu_id = id if (id and id != "approval-menu") else f"approval-menu-{uuid.uuid4().hex[:8]}"
        super().__init__(id=menu_id, classes="approval-menu", **kwargs)

        # Flexibly handle signature: single request, (tool_name, call_id, args), or list
        if isinstance(action_requests, str):
            tool_name = action_requests
            call_id = call_id_or_assistant or ""
            tool_args = args or {}
            self._action_requests: list[dict[str, Any]] = [{"name": tool_name, "call_id": call_id, "args": tool_args}]
            self._assistant_id = None
        elif isinstance(action_requests, dict):
            req_dict = dict(action_requests)
            if "call_id" not in req_dict and call_id_or_assistant:
                req_dict["call_id"] = call_id_or_assistant
            self._action_requests = [req_dict]
            self._assistant_id = call_id_or_assistant
        elif isinstance(action_requests, list):
            self._action_requests = []
            for req in action_requests:
                req_dict = dict(req) if isinstance(req, dict) else {"name": str(req)}
                if "call_id" not in req_dict and call_id_or_assistant:
                    req_dict["call_id"] = call_id_or_assistant
                self._action_requests.append(req_dict)
            self._assistant_id = call_id_or_assistant
        else:
            self._action_requests = []
            self._assistant_id = call_id_or_assistant

        self._tool_names: list[str] = [
            str(r.get("name") or r.get("action", "unknown")) for r in self._action_requests
        ]

        # Determine risk: assess from requests if not explicitly high/prod
        first_tool = self._tool_names[0] if self._tool_names else ""
        first_args = (
            self._action_requests[0].get("args", {})
            if self._action_requests
            else {}
        )
        computed_risk = assess_tool_risk(
            first_tool,
            first_args if isinstance(first_args, dict) else {},
            is_prod=is_prod,
        )
        if risk and risk not in ("low", ""):
            self._risk = risk
        else:
            self._risk = computed_risk

        self.add_class(f"risk-{self._risk}")
        self._is_auto_fallback = any(
            isinstance(request.get("description"), str)
            and request["description"].startswith("Auto human fallback ")
            for request in self._action_requests
        )
        self._show_auto_option = self._is_auto_fallback or auto_mode_eligible
        self._options = self._build_options()
        self._num_options = len(self._options)
        self._reject_index = self._num_options - 1
        self._selected = 0
        self._future: asyncio.Future[dict[str, str]] | None = None
        self._option_widgets: list[Static] = []
        self._tool_info_container: Vertical | None = None
        self._is_minimal = all(name in self._MINIMAL_TOOLS for name in self._tool_names)
        self._command_expanded = False
        self._command_widget: Static | None = None
        self._has_expandable_command = self._check_expandable_command()
        self._security_warnings = self._collect_security_warnings()
        self._reason_input: Input | None = None
        self._reason_input_active = False
        self._help_widget: Static | None = None

    def set_future(self, future: asyncio.Future[dict[str, str]]) -> None:
        """Set the future to resolve when user decides."""
        self._future = future

    def _check_expandable_command(self) -> bool:
        """Check if there's a shell command that can be expanded."""
        if len(self._action_requests) != 1:
            return False
        req = self._action_requests[0]
        if req.get("name", "") not in self._MINIMAL_TOOLS:
            return False
        command = str(req.get("args", {}).get("command", "") or req.get("args", {}).get("CommandLine", ""))
        return _is_command_too_long(command)

    def _build_border_title(self) -> str:
        return " Tool Approval "

    def _format_option_display(self, text: str) -> str:
        """Format option text for card display."""
        if text == "Approve (y)":
            return "(y) Approve once"
        if text.startswith("Approve all ") and text.endswith(" (y)"):
            return f"(y) {text[:-4]}"
        if text == "Enable Smart mode for this thread (s)":
            return "(s) Enable Smart mode for this session"
        if text == "Switch to Manual (s)":
            return "(s) Switch to Manual mode"
        if text == "Reject (n)":
            return "(n) Reject"
        if text.startswith("Reject all ") and text.endswith(" (n)"):
            return f"(n) {text[:-4]}"
        return text

    def _get_command_display(self, *, expanded: bool) -> Content:
        """Get the command display content (truncated or full)."""
        if not self._action_requests:
            raise RuntimeError("_get_command_display called with empty action_requests")
        req = self._action_requests[0]
        command_raw = str(req.get("args", {}).get("command", "") or req.get("args", {}).get("CommandLine", ""))
        command = strip_dangerous_unicode(command_raw)
        issues = detect_dangerous_unicode(command_raw)

        too_long = _is_command_too_long(command)
        if expanded or not too_long:
            command_display = command
        else:
            command_display = _truncate_command(command)

        display = highlight_command(command_display)
        if not expanded and too_long:
            display = Content.assemble(
                display,
                Content.from_markup("  [dim](press 'e' to expand)[/dim]"),
            )

        if not issues:
            return display

        raw_with_markers = render_with_unicode_markers(command_raw)
        if not expanded and len(raw_with_markers) > _WARNING_TEXT_TRUNCATE_LENGTH:
            raw_with_markers = (
                raw_with_markers[:_WARNING_TEXT_TRUNCATE_LENGTH] + get_glyphs().ellipsis
            )

        return Content.assemble(
            display,
            Content.from_markup(
                "\n[$warning]Warning:[/$warning] hidden chars detected ($summary)\n"
                "[dim]raw: $raw[/dim]",
                summary=summarize_issues(issues),
                raw=raw_with_markers,
            ),
        )

    def compose(self) -> ComposeResult:
        """Compose the widget."""
        yield Static("", classes="approval-title")

        if self._security_warnings:
            parts: list[Content] = [
                Content.from_markup(
                    "[$warning]Warning:[/$warning] Potentially deceptive text"
                ),
            ]
            parts.extend(
                Content.from_markup("\n[dim]- $w[/dim]", w=warning)
                for warning in self._security_warnings[:_WARNING_PREVIEW_LIMIT]
            )
            if len(self._security_warnings) > _WARNING_PREVIEW_LIMIT:
                remaining = len(self._security_warnings) - _WARNING_PREVIEW_LIMIT
                parts.append(Content.styled(f"\n- +{remaining} more warning(s)", "dim"))
            yield Static(
                Content.assemble(*parts),
                classes="approval-security-warning",
            )

        if self._is_minimal and len(self._action_requests) == 1:
            req = self._action_requests[0]
            cmd = str(req.get("args", {}).get("command", "") or req.get("args", {}).get("CommandLine", ""))
            first_line = cmd.strip().split("\n")[0] if cmd.strip() else "execute"
            if len(first_line) > 60:
                first_line = first_line[:57] + get_glyphs().ellipsis

            with Vertical(classes="approval-header-info"):
                yield Static(
                    Content.from_markup("  [dim]Tool:[/dim]   [bold]$name[/bold]", name=self._tool_names[0]),
                    classes="approval-meta-line",
                )
                yield Static(
                    Content.from_markup("  [dim]Target:[/dim] $target", target=first_line),
                    classes="approval-meta-line",
                )

            with Container(classes="approval-command-box") as box:
                box.border_title = " Command "
                self._command_widget = Static(
                    self._get_command_display(expanded=self._command_expanded),
                    classes="approval-command",
                )
                yield self._command_widget

        if not self._is_minimal:
            with Vertical(classes="approval-header-info"):
                count = len(self._action_requests)
                if count == 1:
                    yield Static(
                        Content.from_markup("  [dim]Tool:[/dim]   [bold]$name[/bold]", name=self._tool_names[0]),
                        classes="approval-meta-line",
                    )
                else:
                    yield Static(
                        Content.from_markup("  [dim]Tools:[/dim]  [bold]$count tool calls require approval[/bold]", count=count),
                        classes="approval-meta-line",
                    )

            with VerticalScroll(classes="tool-info-scroll"):
                self._tool_info_container = Vertical(classes="tool-info-container")
                yield self._tool_info_container

            glyphs = get_glyphs()
            yield Static(glyphs.box_horizontal * 40, classes="approval-separator")

        self._option_widgets.clear()
        with Container(classes="approval-options-container"):
            for i in range(self._num_options):
                widget = Static("", classes="approval-option")
                self._option_widgets.append(widget)
                yield widget

        self._reason_input = Input(
            placeholder="Reason (Enter to submit, Esc to cancel)",
            classes="approval-reason-input",
            id="approval-reason-input",
        )
        self._reason_input.display = False
        yield self._reason_input

        self._help_widget = Static(self._compose_help_text(), classes="approval-help")
        yield self._help_widget

    def _has_expandable_diff(self) -> bool:
        """Return whether any mounted tool approval widget has an expandable diff."""
        if not self._tool_info_container:
            return False
        for child in self._tool_info_container.children:
            if getattr(child, "has_expandable_diff", False):
                return True
        return False

    def _compose_help_text(self) -> str:
        """Build the help-line content for bottom border subtitle."""
        glyphs = get_glyphs()
        sep = f" {glyphs.bullet} "
        if self._reason_input_active:
            return f"enter submit{sep}esc cancel{sep}leave blank to reject without a reason"

        quick_keys = "y/s/n" if self._show_auto_option else "y/n"
        parts = ["y approve", "s smart" if self._show_auto_option else "", "n reject"]
        parts = [p for p in parts if p]

        if self._selected == self._reject_index:
            parts.append("tab reason")
        if self._has_expandable_command:
            parts.append("e expand")
        if self._has_expandable_diff():
            parts.append("f full diff")

        return sep.join(parts) + f" ({quick_keys} quick keys)"

    async def on_mount(self) -> None:
        """Focus self on mount, set border chrome, and update tool info."""
        self.border_title = self._build_border_title()
        self.border_subtitle = f" {self._compose_help_text()} "

        if is_ascii_mode():
            colors = theme.get_theme_colors(self)
            self.styles.border = ("ascii", colors.warning)

        if not self._is_minimal:
            await self._update_tool_info()
        self._update_options()
        self.focus()

    async def _update_tool_info(self) -> None:
        """Mount tool-specific approval widgets."""
        if not self._tool_info_container:
            return

        await self._tool_info_container.remove_children()

        for i, action_request in enumerate(self._action_requests):
            tool_name: str = str(action_request.get("name") or action_request.get("action", "unknown"))
            tool_args_raw = action_request.get("args", {})
            tool_args: dict[str, Any] = tool_args_raw if isinstance(tool_args_raw, dict) else {}

            if len(self._action_requests) > 1:
                header = Static(
                    Content.from_markup(
                        "[bold]$num. $name[/bold]",
                        num=i + 1,
                        name=tool_name,
                    )
                )
                await self._tool_info_container.mount(header)

            description = action_request.get("description")
            if description:
                desc_str = str(description).strip()
                file_target = str(
                    tool_args.get("file_path")
                    or tool_args.get("TargetFile")
                    or tool_args.get("path")
                    or ""
                )
                is_redundant = bool(file_target) and (
                    desc_str == f"Write file: {file_target}"
                    or desc_str == f"Edit file: {file_target}"
                    or desc_str == file_target
                )
                if not is_redundant:
                    desc_widget = Static(
                        Content.from_markup("[dim]$desc[/dim]", desc=desc_str),
                        classes="approval-description",
                    )
                    await self._tool_info_container.mount(desc_widget)

            renderer = get_renderer(tool_name)
            widget_class, data = renderer.get_approval_widget(
                tool_args, assistant_id=self._assistant_id
            )
            approval_widget = widget_class(data)
            await self._tool_info_container.mount(approval_widget)

    def _build_options(self) -> list[tuple[str, str]]:
        """Build visible options."""
        count = len(self._action_requests)
        approve = "Approve (y)" if count == 1 else f"Approve all {count} (y)"
        reject = "Reject (n)" if count == 1 else f"Reject all {count} (n)"
        options: list[tuple[str, str]] = [(approve, "approve")]
        if self._show_auto_option:
            if self._is_auto_fallback:
                options.append(("Switch to Manual (s)", "switch_manual"))
            else:
                options.append(("Enable Smart mode for this thread (s)", "smart_approve_all"))
        options.append((reject, "reject"))
        return options

    def _update_options(self) -> None:
        """Update option widgets based on selection."""
        bar = "▌" if not is_ascii_mode() else "|"
        for i, ((text, _decision), widget) in enumerate(
            zip(self._options, self._option_widgets)
        ):
            display_text = self._format_option_display(text)
            escaped_display = escape(display_text)
            if i == self._selected:
                cursor = f"[bold $primary]{bar}[/bold $primary] "
                widget.update(Content.from_markup(f"{cursor}{escaped_display}"))
                widget.add_class("approval-option-selected")
            else:
                cursor = "  "
                widget.update(Content.from_markup(f"{cursor}{escaped_display}"))
                widget.remove_class("approval-option-selected")

        if self._help_widget is not None:
            self._help_widget.update(self._compose_help_text())
        self.border_subtitle = f" {self._compose_help_text()} "

    def action_move_up(self) -> None:
        """Move selection up."""
        if self._reason_input_active:
            return
        self._selected = (self._selected - 1) % self._num_options
        self._update_options()

    def action_move_down(self) -> None:
        """Move selection down."""
        if self._reason_input_active:
            return
        self._selected = (self._selected + 1) % self._num_options
        self._update_options()

    def action_select(self) -> None:
        """Select current option."""
        self._handle_selection(self._selected)

    def action_select_position(self, position: int) -> None:
        """Submit option at position."""
        if not 0 <= position < self._num_options:
            return
        self._handle_selection(position)

    def action_select_approve(self) -> None:
        """Submit approve option."""
        self._handle_selection(0)

    def action_select_smart(self) -> None:
        """Submit smart option if available."""
        if not self._show_auto_option:
            return
        self._handle_selection(1)

    def action_select_auto(self) -> None:
        """Submit auto/smart option if available (alias to action_select_smart)."""
        self.action_select_smart()

    def action_select_reject(self) -> None:
        """Submit reject option."""
        if self._reason_input_active:
            self._exit_reason_input_mode()
            return
        self._handle_selection(self._reject_index)

    def action_toggle_expand(self) -> None:
        """Toggle shell command expansion."""
        if not self._has_expandable_command or not self._command_widget:
            return
        self._command_expanded = not self._command_expanded
        self._command_widget.update(
            self._get_command_display(expanded=self._command_expanded)
        )

    def action_toggle_full_diff(self) -> None:
        """Toggle full diff expansion on any mounted file approval widget."""
        if self._reason_input_active or not self._tool_info_container:
            return
        toggled = False
        for child in self._tool_info_container.children:
            toggle_fn = getattr(child, "toggle_full_diff", None)
            if callable(toggle_fn):
                if toggle_fn():
                    toggled = True
        if toggled:
            self._update_options()

    @on(events.Click, ".approval-option")
    def _on_option_clicked(self, event: events.Click) -> None:
        """Handle clicks on specific approval option widgets."""
        event.stop()
        for idx, widget in enumerate(self._option_widgets):
            if event.control == widget:
                self._handle_selection(idx)
                break

    @on(events.Click, ".diff-line-truncated")
    def _on_diff_truncated_clicked(self, event: events.Click) -> None:
        """Expand full diff when the truncation footer is clicked."""
        event.stop()
        self.action_toggle_full_diff()

    def on_click(self, event: events.Click) -> None:
        """Handle clicks anywhere on option widgets or menu options container."""
        for idx, widget in enumerate(self._option_widgets):
            if event.control == widget or (widget.is_mounted and event.get_content_offset(widget) is not None):
                self._handle_selection(idx)
                event.stop()
                return

    def _handle_selection(
        self, option: int, *, reject_message: str | None = None
    ) -> None:
        """Handle the selected option."""
        decision_type = self._options[option][1]
        decision: dict[str, str] = {"type": decision_type}
        if decision_type == "reject" and reject_message:
            decision["message"] = reject_message

        self.display = False

        if self._future and not self._future.done():
            self._future.set_result(decision)

        first_req = self._action_requests[0] if self._action_requests else {}
        tool_name: str = str(first_req.get("name") or first_req.get("action", ""))
        call_id: str = str(first_req.get("call_id") or self._assistant_id or "")
        approved = decision_type in {"approve", "auto_approve_all", "smart_approve_all"}

        self.post_message(
            self.Decided(
                decision=decision,
                approved=approved,
                tool_name=tool_name,
                call_id=call_id,
                comment=reject_message or "",
            )
        )

    def action_reject_with_reason(self) -> None:
        """Enter free-text reject mode."""
        if self._reason_input_active:
            return
        if self._selected != self._reject_index:
            return
        if self._reason_input is None:
            logger.warning("action_reject_with_reason: _reason_input is None")
            return
        self._reason_input_active = True
        self._reason_input.value = ""
        self._reason_input.display = True
        if self._help_widget is not None:
            self._help_widget.update(self._compose_help_text())
        if self.is_mounted:
            self._reason_input.focus()

    def _exit_reason_input_mode(self) -> None:
        """Close reason input mode."""
        if not self._reason_input_active or self._reason_input is None:
            return
        self._reason_input_active = False
        self._reason_input.display = False
        if self._help_widget is not None:
            self._help_widget.update(self._compose_help_text())
        if self.is_mounted:
            self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit reject decision with typed reason."""
        if event.input is not self._reason_input:
            return
        event.stop()
        if not self._reason_input_active:
            return
        reason = event.value.strip()
        self._reason_input_active = False
        self._handle_selection(self._reject_index, reject_message=reason or None)

    def _collect_security_warnings(self) -> list[str]:
        """Collect security warnings for Unicode and URLs."""
        warnings: list[str] = []
        for action_request in self._action_requests:
            tool_name = str(action_request.get("name", "unknown"))
            args = action_request.get("args", {})
            if not isinstance(args, dict):
                continue
            for arg_path, text in iter_string_values(args):
                issues = detect_dangerous_unicode(text)
                if issues:
                    warnings.append(
                        f"{tool_name}.{arg_path}: hidden Unicode "
                        f"({summarize_issues(issues)})"
                    )
                if looks_like_url_key(arg_path):
                    result = check_url_safety(text)
                    if result.safe:
                        continue
                    detail = format_warning_detail(result.warnings)
                    if result.decoded_domain:
                        detail = f"{detail}; decoded host: {result.decoded_domain}"
                    warnings.append(f"{tool_name}.{arg_path}: {detail}")
        return warnings

    def on_blur(self, event: events.Blur) -> None:
        """Re-focus on blur unless reason input is active."""
        if self._reason_input_active or not self.is_mounted:
            return
        self.call_after_refresh(self.focus)


def assess_tool_risk(
    tool_name: str,
    args: dict[str, Any] | None = None,
    is_prod: bool | None = None,
    *,
    aws_profile: str = "",
    k8s_context: str = "",
    tf_workspace: str = "",
) -> str:
    """Assess risk level (low, medium, high, prod) for tool call.

    Automatically upgrades to 'prod' if active cloud context is production.
    """
    args = args or {}
    if is_prod is None:
        from opscloud.ui.context_detection import current_k8s_context, detect_production

        p_aws = (
            aws_profile
            or os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_DEFAULT_PROFILE")
            or ""
        )
        p_k8s = k8s_context or current_k8s_context()
        p_tf = tf_workspace or os.environ.get("TF_WORKSPACE", "")
        source = detect_production(aws_profile=p_aws, k8s_context=p_k8s, tf_workspace=p_tf)
        is_prod = source is not None

    if is_prod:
        return "prod"

    name = tool_name.lower()
    cmd = str(
        args.get("command", "")
        or args.get("CommandLine", "")
        or args.get("raw_command", "")
    ).lower()

    destructive_keywords = (
        "destroy",
        "delete",
        "apply",
        "drop",
        "terminate",
        "rm -rf",
        "rm -r",
        "drop database",
    )
    if any(k in name or k in cmd for k in destructive_keywords):
        return "high"
    if any(k in name or k in cmd for k in ("write", "replace", "patch", "edit")):
        return "medium"
    return "low"


class ApprovalDecided(Message):
    """Fired when user resolves an approval decision."""

    def __init__(
        self,
        approved: bool,
        tool_name: str,
        call_id: str,
        comment: str = "",
    ) -> None:
        super().__init__()
        self.approved = approved
        self.tool_name = tool_name
        self.call_id = call_id
        self.comment = comment


class ApprovalModalScreen(OpsModalScreen[ApprovalDecided]):
    """Full-screen modal screen for high-risk / production tool executions."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    DEFAULT_CSS = """
    ApprovalModalScreen .modal-title {
        display: none;
    }
    ApprovalModalScreen .modal-meta-line {
        height: auto;
        margin-bottom: 1;
    }
    ApprovalModalScreen #modal-args-scroll {
        height: auto;
        max-height: 12;
        background: $background;
        border: round $panel;
        padding: 0 1;
        margin-bottom: 1;
    }
    ApprovalModalScreen #modal-comment {
        height: 3;
        margin-bottom: 1;
        border: round $panel;
    }
    ApprovalModalScreen .modal-buttons {
        layout: horizontal;
        height: auto;
        align-horizontal: right;
    }
    ApprovalModalScreen .modal-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "approve", "Approve", show=False),
        Binding("n", "reject", "Reject", show=False),
        Binding("escape", "reject", "Cancel", show=False),
    ]

    def __init__(
        self,
        tool_name: str,
        call_id: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._tool_name = tool_name
        self._call_id = call_id
        self._args = args

    def on_mount(self) -> None:
        super().on_mount()
        self.set_modal_title("Tool Execution Approval")
        self.set_modal_subtitle([
            ("y", "confirm execution"),
            ("n", "cancel / reject"),
            ("esc", "close"),
        ])

    def compose_modal(self) -> ComposeResult:
        rendered = render_tool_approval(self._tool_name, self._args)
        yield Static(
            Content.from_markup(
                "  [dim]Tool:[/dim] [bold]$name[/bold]",
                name=rendered.title or self._tool_name,
            ),
            classes="modal-meta-line",
        )
        with VerticalScroll(id="modal-args-scroll"):
            if rendered.diff_lines:
                patch = "\n".join(rendered.diff_lines)
                yield from compose_diff_lines(patch)
            else:
                raw_cmd = (
                    self._args.get("command")
                    or self._args.get("CommandLine")
                    or self._args.get("raw_command")
                )
                if raw_cmd:
                    yield Static(highlight_command(str(raw_cmd)))
                else:
                    details = rendered.details or [f"{k}: {v}" for k, v in self._args.items()]
                    yield Static("\n".join(details))
        yield TextArea(placeholder="Rejection reason / comments...", id="modal-comment")
        with Container(classes="modal-buttons"):
            yield Button("CONFIRM EXECUTION (y)", variant="error", id="btn-modal-approve")
            yield Button("CANCEL / REJECT (n)", variant="default", id="btn-modal-reject")

    @on(Button.Pressed, "#btn-modal-approve")
    def action_approve(self) -> None:
        comment = ""
        try:
            comment = self.query_one("#modal-comment", TextArea).text
        except Exception:
            pass
        self.dismiss(ApprovalDecided(True, self._tool_name, self._call_id, comment))

    @on(Button.Pressed, "#btn-modal-reject")
    def action_reject(self) -> None:
        comment = ""
        try:
            comment = self.query_one("#modal-comment", TextArea).text
        except Exception:
            pass
        self.dismiss(ApprovalDecided(False, self._tool_name, self._call_id, comment))


__all__ = [
    "ApprovalDecided",
    "ApprovalMenu",
    "ApprovalModalScreen",
    "assess_tool_risk",
    "get_target_context_pill",
    "highlight_command",
]
