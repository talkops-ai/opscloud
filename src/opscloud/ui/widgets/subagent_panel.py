"""Live panel showing subagents fanned out from within `js_eval` or `task` calls.

When the agent writes code that calls the top-level `task()` global or spawns subagents,
each dispatch runs as a subagent. This widget consumes lifecycle events and renders
a docked, live-updating fan-out panel.
"""

from __future__ import annotations

import contextlib
from opscloud.utils.logger import get_logger
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches, TooManyMatches
from textual.reactive import reactive
from textual.widgets import Static

from opscloud.config.settings import get_glyphs
from opscloud.ui.theme import get_theme_colors
from opscloud.ui.widgets.loading import Spinner

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult
    from textual.timer import Timer

logger = get_logger(__name__)

SubagentStatus = Literal["running", "done", "error", "cancelled"]

_MODEL_COL = 16
_TIMING_COL = 6
_STATUS_COL = 5
_MIN_TASK_COL = 16
_SCROLLBAR_RESERVE = 2
_FALLBACK_WIDTH = 100
_MIN_BODY_HEIGHT = 3
_MAX_BODY_HEIGHT = 12
_AGENTS_CHROME_LINES = 1
_TICK_INTERVAL = 0.1
_LABEL_FALLBACK_MAX_CHARS = 60


def sanitize_control_chars(text: str, *, keep_newlines: bool = False, max_length: int | None = None) -> str:
    """Sanitize control characters for safe TUI display."""
    if not text:
        return ""
    if not keep_newlines:
        text = " ".join(text.splitlines())
    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
    return text


def format_duration(seconds: float) -> str:
    """Format seconds into a short human-readable duration string."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining_sec = int(seconds % 60)
    return f"{minutes}m{remaining_sec}s"


def _right_block_width() -> int:
    gap = 2
    return _MODEL_COL + gap + _TIMING_COL


@dataclass
class _SubagentRecord:
    """One subagent's live state within a phase."""

    id: str
    label: str
    subagent_type: str = ""
    description: str = ""
    status: SubagentStatus = "running"
    started_monotonic: float = field(default_factory=time.monotonic)
    duration_ms: int | None = None
    error: str | None = None
    model: str | None = None
    current_action: str | None = None

    def elapsed_seconds(self) -> float:
        if self.duration_ms is not None:
            return self.duration_ms / 1000
        return max(0.0, time.monotonic() - self.started_monotonic)


@dataclass
class _Phase:
    """One `js_eval` or `task` fan-out batch, keyed by the eval's tool-call id."""

    eval_id: str
    index: int
    records: dict[str, _SubagentRecord] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, record: _SubagentRecord) -> None:
        if record.id not in self.records:
            self.order.append(record.id)
        self.records[record.id] = record

    def replace_id(self, old_id: str, new_id: str, updated_record: _SubagentRecord) -> None:
        """Update record id and order when a subagent is resumed after interrupt."""
        if old_id in self.order:
            idx = self.order.index(old_id)
            self.order[idx] = new_id
        elif new_id not in self.order:
            self.order.append(new_id)

        self.records.pop(old_id, None)
        self.records[new_id] = updated_record
        self.aliases[old_id] = new_id

    def find_record(self, sub_id: str) -> _SubagentRecord | None:
        rec = self.records.get(sub_id)
        if rec is not None:
            return rec
        alias = self.aliases.get(sub_id)
        if alias is not None:
            return self.records.get(alias)
        return None

    def counts(self) -> tuple[int, int]:
        total = len(self.order)
        done = sum(1 for sub_id in self.order if (rec := self.records.get(sub_id)) and rec.status != "running")
        return done, total

    def any_running(self) -> bool:
        return any(r.status == "running" for r in self.records.values())

    def any_error(self) -> bool:
        return any(r.status == "error" for r in self.records.values())

    def any_cancelled(self) -> bool:
        return any(r.status == "cancelled" for r in self.records.values())

    def all_terminal(self) -> bool:
        return bool(self.records) and not self.any_running()

    def elapsed_seconds(self) -> float:
        if not self.records:
            return 0.0
        earliest = min(r.started_monotonic for r in self.records.values())
        if self.all_terminal():
            latest_end = max(
                r.started_monotonic + r.elapsed_seconds() for r in self.records.values()
            )
            return max(0.0, latest_end - earliest)
        return max(0.0, time.monotonic() - earliest)


def _format_timing(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return format_duration(seconds)


def _sanitize(text: str, *, max_chars: int) -> str:
    return sanitize_control_chars(text, keep_newlines=False, max_length=max_chars)


class SubagentPanel(Vertical):
    """Docked two-pane panel visualizing subagent fan-out by phase."""

    can_focus = True
    can_focus_children = False

    DEFAULT_CSS = """
    SubagentPanel {
        height: auto;
        background: $background;
        border-top: solid $panel;
        display: none;
        padding: 1 2;
    }

    SubagentPanel.-collapsed {
        padding: 0 2;
    }

    SubagentPanel.-visible {
        display: block;
    }

    SubagentPanel:focus {
        border-top: solid $primary;
    }

    SubagentPanel #subagent-header {
        width: 1fr;
        height: 1;
        text-style: bold;
    }

    SubagentPanel #subagent-body {
        width: 1fr;
        height: auto;
        margin-top: 1;
    }

    SubagentPanel #subagent-body.-collapsed {
        display: none;
    }

    SubagentPanel #subagent-phases-scroll {
        width: 24;
        height: 100%;
        border-right: solid $panel;
        padding-right: 2;
        margin-right: 2;
    }

    SubagentPanel #subagent-phases-scroll.-hidden {
        display: none;
    }

    SubagentPanel #subagent-agents-scroll {
        width: 1fr;
        height: 100%;
    }
    """

    expanded: reactive[bool] = reactive(default=True, init=False)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._phases: dict[str, _Phase] = {}
        self._phase_order: list[str] = []
        self._active_eval_id: str | None = None
        self._selected_eval_id: str | None = None
        self._model_label: str | None = None
        self._applied_height: int | None = None
        self._last_render: dict[str, str] = {}
        self._spinner = Spinner()
        self._timer: Timer | None = None
        self._pending_reset = False

    def compose(self) -> ComposeResult:
        yield Static("", id="subagent-header", markup=False)
        with Horizontal(id="subagent-body"):
            with VerticalScroll(id="subagent-phases-scroll"):
                yield Static("", id="subagent-phases", markup=False)
            with VerticalScroll(id="subagent-agents-scroll"):
                yield Static("", id="subagent-agents", markup=False)

    @property
    def _active_phase(self) -> _Phase | None:
        if self._active_eval_id is None:
            return self._phases.get("")
        return self._phases.get(self._active_eval_id)

    def _displayed_phase(self) -> _Phase | None:
        if self._selected_eval_id is not None:
            phase = self._phases.get(self._selected_eval_id)
            if phase is not None:
                return phase
        return self._active_phase

    def spawn_subagent(self, agent_name: str, task: str) -> None:
        """Backwards compatibility helper for spawning subagents directly."""
        event = {
            "type": "subagent",
            "phase": "start",
            "id": f"subagent-{agent_name}-{time.time()}",
            "subagent_type": agent_name,
            "description": task,
        }
        self.on_subagent_event(event)

    def append_token(self, agent_name: str, token: str) -> None:
        """Backwards compatibility helper for token streaming."""
        pass

    def finish_subagent(self, agent_name: str) -> None:
        """Backwards compatibility helper for finishing subagent."""
        record = self._find_record_by_name(agent_name)
        if record:
            event = {
                "type": "subagent",
                "phase": "complete",
                "id": record.id,
            }
            self.on_subagent_event(event)

    def _find_record_by_name(self, agent_name: str) -> _SubagentRecord | None:
        clean_name = agent_name.split("@")[0] if "@" in agent_name else agent_name
        for phase in self._phases.values():
            for record in phase.records.values():
                if (
                    record.id == agent_name
                    or record.subagent_type == clean_name
                    or clean_name in record.subagent_type
                    or clean_name in record.label
                    or agent_name in record.label
                ):
                    return record
        return None

    def on_subagent_event(self, event: dict[str, Any]) -> None:
        """Apply one validated subagent lifecycle event."""
        phase = event.get("phase")
        sub_id = event.get("id")
        if not isinstance(sub_id, str) or not sub_id:
            logger.debug("Dropping subagent event with missing/invalid id: %r", event)
            return
        eval_id = event.get("eval_id")
        eval_key = eval_id if isinstance(eval_id, str) else ""

        if phase == "start":
            self._handle_start(sub_id, eval_key, event)
        elif phase in {"complete", "error"}:
            self._handle_finish(sub_id, eval_key, phase, event)
        else:
            return

        self._refresh()

    def _resolve_subagent_model(self, subagent_type: str | None) -> str | None:
        if not subagent_type:
            return None
        try:
            from opscloud.subagents.loader import list_subagents

            for meta in list_subagents():
                name = meta.get("name")
                alias = meta.get("alias")
                name_str = name if isinstance(name, str) else ""
                alias_str = alias if isinstance(alias, str) else ""
                if (
                    subagent_type in {name_str, alias_str}
                    or (name_str and name_str.startswith(subagent_type))
                    or (alias_str and alias_str.startswith(subagent_type))
                ):
                    m = meta.get("model")
                    if isinstance(m, str) and m:
                        return m
                    if m is not None:
                        return str(m)
        except Exception:
            pass
        return None

    def set_subagent_activity(self, sub_id: str | None, action: str | None) -> None:
        """Update live activity/tool description for a subagent."""
        if sub_id:
            record = self._find_record(sub_id) or self._find_record_by_name(sub_id)
            if record:
                record.current_action = action
                self._refresh()
                return
        for phase in self._phases.values():
            for record in phase.records.values():
                if record.status == "running":
                    record.current_action = action
                    self._refresh()
                    return

    def set_subagent_model(self, sub_id: str | None, model: str | None) -> None:
        """Update model description for a subagent."""
        if not model:
            return
        clean_model = model.split(":")[-1] if ":" in model else model
        if sub_id:
            record = self._find_record(sub_id) or self._find_record_by_name(sub_id)
            if record:
                record.model = _sanitize(clean_model, max_chars=60)
                self._refresh()
                return
        for phase in self._phases.values():
            for record in phase.records.values():
                if record.status == "running":
                    record.model = _sanitize(clean_model, max_chars=60)
                    self._refresh()
                    return

    def _handle_start(self, sub_id: str, eval_key: str, event: dict[str, Any]) -> None:
        if self._pending_reset:
            self._clear()
        phase = self._ensure_phase(eval_key)
        self._active_eval_id = eval_key

        sub_model = event.get("model")
        raw_sub_type = str(event.get("subagent_type") or "")
        clean_sub_type = raw_sub_type.split("@")[0] if "@" in raw_sub_type else raw_sub_type
        description = str(event.get("description") or "")

        if not sub_model:
            sub_model = self._resolve_subagent_model(raw_sub_type) or self._resolve_subagent_model(clean_sub_type)
        if not sub_model:
            sub_model = self._model_label
        if not sub_model:
            try:
                if hasattr(self, "app") and self.app is not None:
                    sub_model = getattr(self.app, "_model", None)
            except Exception:
                pass

        if sub_model and ":" in sub_model:
            sub_model = sub_model.split(":")[-1]

        label = _sanitize(self._row_label(event), max_chars=200)

        # Check if this subagent was already running in this phase (e.g. resumed from HITL approval interrupt)
        existing_record: _SubagentRecord | None = None
        for rec in phase.records.values():
            if rec.status == "running":
                if rec.label == label or (
                    clean_sub_type and rec.subagent_type == clean_sub_type and description and rec.description == description
                ):
                    existing_record = rec
                    break

        if existing_record is not None and existing_record.id != sub_id:
            # Resumed subagent: update ID and preserve continuity without duplicate row
            old_id = existing_record.id
            existing_record.id = sub_id
            if sub_model:
                existing_record.model = _sanitize(sub_model, max_chars=60)
            phase.replace_id(old_id, sub_id, existing_record)
        else:
            record = _SubagentRecord(
                id=sub_id,
                label=label,
                subagent_type=clean_sub_type,
                description=description,
                model=_sanitize(sub_model, max_chars=60) if sub_model else None,
            )
            phase.add(record)

        self._show()
        self._apply_body_height()
        self._ensure_timer()

    def _ensure_phase(self, eval_key: str) -> _Phase:
        phase = self._phases.get(eval_key)
        if phase is None:
            phase = _Phase(eval_id=eval_key, index=len(self._phase_order) + 1)
            self._phases[eval_key] = phase
            self._phase_order.append(eval_key)
        return phase

    @staticmethod
    def _row_label(event: dict[str, Any]) -> str:
        sub_type = str(event.get("subagent_type") or "subagent")
        # Clean plugin suffix if present (e.g. aws-finops-agent@talkops-devops-plugins -> aws-finops-agent)
        if "@" in sub_type:
            sub_type = sub_type.split("@")[0]

        # Prefer full description over pre-truncated label to prevent awkward mid-word cutoffs
        description = event.get("description")
        label = event.get("label")
        if isinstance(description, str) and description.strip():
            clean_desc = " ".join(description.split())
            text = clean_desc
        elif isinstance(label, str) and label.strip():
            clean_label = " ".join(label.split())
            text = clean_label
        else:
            text = ""

        if len(text) > 100:
            text = text[:97] + "..."

        return f"{sub_type}: {text}" if text else sub_type

    def _handle_finish(
        self, sub_id: str, eval_key: str, outcome: str, event: dict[str, Any]
    ) -> None:
        record = self._find_record(sub_id)
        if record is None:
            if outcome != "error":
                return
            record = self._adopt_orphan_finish(sub_id, eval_key, event)
        record.status = "done" if outcome == "complete" else "error"
        record.current_action = None
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)):
            record.duration_ms = int(duration)
        if outcome == "error":
            raw_err = event.get("error")
            record.error = (
                _sanitize(raw_err, max_chars=120) if isinstance(raw_err, str) else None
            )
        if not self._any_running():
            self._stop_timer()

    def _adopt_orphan_finish(
        self, sub_id: str, eval_key: str, event: dict[str, Any]
    ) -> _SubagentRecord:
        if self._pending_reset:
            self._clear()
        phase = self._ensure_phase(eval_key)
        self._active_eval_id = eval_key
        record = _SubagentRecord(
            id=sub_id,
            label=_sanitize(self._row_label(event), max_chars=200),
        )
        phase.add(record)
        self._show()
        self._apply_body_height()
        return record

    def _find_record(self, sub_id: str) -> _SubagentRecord | None:
        for phase in self._phases.values():
            record = phase.find_record(sub_id)
            if record is not None:
                return record
        return None

    def _any_running(self) -> bool:
        return any(phase.any_running() for phase in self._phases.values())

    def on_click(self, event: events.Click) -> None:
        if self._header_clicked(event):
            self.toggle()
            event.stop()
            return
        if len(self._phase_order) <= 1:
            return
        row = self._clicked_phase_row(event)
        if row is not None and 0 <= row < len(self._phase_order):
            self._selected_eval_id = self._phase_order[row]
            self._refresh()
            event.stop()

    def _header_clicked(self, event: events.Click) -> bool:
        try:
            header = self.query_one("#subagent-header", Static)
        except (NoMatches, TooManyMatches):
            return False
        return event.get_content_offset(header) is not None

    def _clicked_phase_row(self, event: events.Click) -> int | None:
        try:
            phases = self.query_one("#subagent-phases", Static)
        except (NoMatches, TooManyMatches):
            return None
        offset = event.get_content_offset(phases)
        if offset is None:
            return None
        return offset.y - 1

    def on_key(self, event: events.Key) -> None:
        if len(self._phase_order) <= 1:
            return
        if event.key in {"down", "j"}:
            self._move_selection(1)
            event.stop()
        elif event.key in {"up", "k"}:
            self._move_selection(-1)
            event.stop()

    def _move_selection(self, delta: int) -> None:
        if not self._phase_order:
            return
        current = self._displayed_phase()
        current_key = current.eval_id if current else self._phase_order[0]
        try:
            index = self._phase_order.index(current_key)
        except ValueError:
            index = 0
        new_index = max(0, min(len(self._phase_order) - 1, index + delta))
        self._selected_eval_id = self._phase_order[new_index]
        self._refresh()

    def prepare_turn(self, *, model_label: str | None = None) -> None:
        self._model_label = (
            _sanitize(model_label, max_chars=_MODEL_COL) if model_label else None
        )
        if self._any_running():
            self._clear()
        else:
            self._pending_reset = True

    def reset(self, *, model_label: str | None = None, **_kwargs: Any) -> None:
        self._clear()
        self._model_label = (
            _sanitize(model_label, max_chars=_MODEL_COL) if model_label else None
        )

    def _clear(self) -> None:
        self._phases.clear()
        self._phase_order.clear()
        self._active_eval_id = None
        self._selected_eval_id = None
        self._applied_height = None
        self._last_render.clear()
        self._pending_reset = False
        self._stop_timer()
        self.remove_class("-visible")

    def finalize_running(self) -> None:
        changed = False
        for phase in self._phases.values():
            for record in phase.records.values():
                if record.status == "running":
                    record.status = "cancelled"
                    if record.duration_ms is None:
                        record.duration_ms = int(record.elapsed_seconds() * 1000)
                    changed = True
        if not changed:
            return
        self._stop_timer()
        self._refresh()

    def _show(self) -> None:
        self.add_class("-visible")

    def toggle(self) -> None:
        self.expanded = not self.expanded

    def watch_expanded(self, expanded: bool) -> None:
        try:
            body = self.query_one("#subagent-body")
        except (NoMatches, TooManyMatches):
            return
        body.set_class(not expanded, "-collapsed")
        self.set_class(not expanded, "-collapsed")
        if expanded:
            self._apply_body_height()
        self._refresh()
        if expanded:
            self.call_after_refresh(self._refresh)

    def on_resize(self, _event: events.Resize) -> None:
        self._refresh()

    def _ensure_timer(self) -> None:
        if self._timer is None:
            try:
                self._timer = self.set_interval(_TICK_INTERVAL, self._refresh)
            except RuntimeError:
                pass

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def pause(self) -> None:
        """Pause the refresh timer while an approval dialog is visible."""
        self._stop_timer()

    def resume(self) -> None:
        """Resume the refresh timer after an approval decision."""
        if self._any_running():
            self._ensure_timer()

    def _turn_counts(self) -> tuple[int, int, int, int]:
        total = done = failed = cancelled = 0
        for phase in self._phases.values():
            for record in phase.records.values():
                total += 1
                if record.status != "running":
                    done += 1
                if record.status == "error":
                    failed += 1
                elif record.status == "cancelled":
                    cancelled += 1
        return done, total, failed, cancelled

    def _body_height(self) -> int:
        if not self._phases:
            return _MIN_BODY_HEIGHT
        max_rows = max(len(p.records) for p in self._phases.values())
        agents_lines = _AGENTS_CHROME_LINES + max_rows
        phases_lines = 1 + len(self._phases)
        return min(_MAX_BODY_HEIGHT, max(_MIN_BODY_HEIGHT, agents_lines, phases_lines))

    def _apply_body_height(self) -> None:
        if not self.expanded:
            return
        height = self._body_height()
        if height == self._applied_height:
            return
        with contextlib.suppress(NoMatches, TooManyMatches):
            self.query_one("#subagent-body").styles.height = height
            self._applied_height = height

    def _update_cached(self, widget_id: str, content: Content) -> None:
        if self._last_render.get(widget_id) == content.plain:
            return
        try:
            self.query_one(f"#{widget_id}", Static).update(content)
        except (NoMatches, TooManyMatches):
            return
        self._last_render[widget_id] = content.plain

    def _refresh(self) -> None:
        self._refresh_header()
        self._refresh_phases()
        self._refresh_agents()

    def _refresh_header(self) -> None:
        colors = get_theme_colors(self)
        glyphs = get_glyphs()
        caret = (
            glyphs.disclosure_expanded if self.expanded else glyphs.disclosure_collapsed
        )
        done, total, failed, cancelled = self._turn_counts()
        if self._any_running() or not total:
            icon, tint = self._spinner.next_frame(), colors.warning
        elif failed:
            icon, tint = glyphs.error, colors.error
        elif cancelled:
            icon, tint = glyphs.circle_empty, colors.muted
        else:
            icon, tint = glyphs.checkmark, colors.success
        lead_text = f"{caret} {icon}  dynamic subagents"
        parts: list[Content] = [Content.styled(lead_text, tint)]
        left_len = len(lead_text)

        if self.expanded and total:
            meta = self._header_meta_parts(done, total, failed, cancelled, colors)
            parts.extend(meta)
            left_len += sum(len(p.plain) for p in meta)
        hint = "click to collapse" if self.expanded else "click to expand"
        spacer = max(2, self._header_width() - left_len - len(hint))
        parts.append(Content.styled(" " * spacer + hint, colors.muted))
        self._update_cached("subagent-header", Content.assemble(*parts))

    def _header_meta_parts(
        self,
        done: int,
        total: int,
        failed: int,
        cancelled: int,
        colors: Any,
    ) -> list[Content]:
        glyphs = get_glyphs()
        separator = getattr(glyphs, "separator", "·")
        meta_text = f"   {done}/{total} done"
        count = len(self._phase_order)
        if count:
            plural = "phase" if count == 1 else "phases"
            meta_text += f"  {separator}  {count} {plural}"
        parts: list[Content] = [Content.styled(meta_text, colors.muted)]
        if failed:
            parts.append(Content.styled(f"  {separator}  {failed} failed", colors.error))
        if cancelled:
            parts.append(Content.styled(f"  {separator}  {cancelled} cancelled", colors.muted))
        return parts

    def _header_width(self) -> int:
        try:
            width = self.query_one("#subagent-header", Static).size.width
        except (NoMatches, TooManyMatches):
            width = 0
        return width if width and width > 0 else _FALLBACK_WIDTH

    def _refresh_phases(self) -> None:
        try:
            scroll = self.query_one("#subagent-phases-scroll")
        except (NoMatches, TooManyMatches):
            return
        if not self._phase_order:
            scroll.add_class("-hidden")
            self._update_cached("subagent-phases", Content(""))
            return
        scroll.remove_class("-hidden")
        colors = get_theme_colors(self)
        displayed = self._displayed_phase()
        displayed_key = displayed.eval_id if displayed else None
        rows: list[Content] = [Content.styled("Phases", colors.muted)]
        rows.extend(
            self._phase_row(self._phases[key], selected=key == displayed_key, colors=colors)
            for key in self._phase_order
        )
        self._update_cached("subagent-phases", Content("\n").join(rows))

    def _phase_row(self, phase: _Phase, *, selected: bool, colors: Any) -> Content:
        glyphs = get_glyphs()
        done, total = phase.counts()
        if phase.all_terminal():
            if phase.any_error():
                mark = glyphs.error
            elif phase.any_cancelled():
                mark = glyphs.circle_empty
            else:
                mark = glyphs.checkmark
        elif phase.eval_id == self._active_eval_id:
            mark = glyphs.disclosure_collapsed
        else:
            mark = glyphs.bullet
        caret = glyphs.cursor if selected else " "
        tint = colors.primary if selected else colors.muted
        elapsed = _format_timing(phase.elapsed_seconds())
        sep = getattr(glyphs, "separator", "·")
        return Content.styled(
            f"{caret} {mark} {phase.index} {done}/{total} {sep} {elapsed}", tint
        )

    def _agents_width(self) -> int:
        try:
            width = self.query_one("#subagent-agents", Static).size.width
        except (NoMatches, TooManyMatches):
            width = 0
        if not width or width <= 0:
            width = _FALLBACK_WIDTH
        return max(_MIN_TASK_COL, width - _SCROLLBAR_RESERVE)

    def _task_col(self) -> int:
        width = self._agents_width()
        return max(_MIN_TASK_COL, width - _STATUS_COL - _right_block_width())

    def _refresh_agents(self) -> None:
        phase = self._displayed_phase()
        glyphs = get_glyphs()
        colors = get_theme_colors(self)
        rows: list[Content] = []
        if phase is not None and phase.order:
            task_col = self._task_col()
            rows.append(self._heading_row(task_col, colors))
            rows.extend(
                self._render_row(record, task_col, glyphs, colors)
                for sub_id in phase.order
                if (record := phase.find_record(sub_id)) is not None
            )
        self._update_cached(
            "subagent-agents", Content("\n").join(rows) if rows else Content("")
        )

    @staticmethod
    def _right_block(model: str, timing: str) -> str:
        return (
            f"{model[:_MODEL_COL].ljust(_MODEL_COL)}  "
            f"{timing[:_TIMING_COL].rjust(_TIMING_COL)}"
        )

    def _render_row(
        self,
        record: _SubagentRecord,
        task_col: int,
        glyphs: Any,
        colors: Any,
    ) -> Content:
        if record.status == "running":
            icon = self._spinner.current_frame()
            tint = colors.warning
        elif record.status == "done":
            icon = glyphs.checkmark
            tint = colors.success
        elif record.status == "cancelled":
            icon = glyphs.circle_empty
            tint = colors.muted
        else:
            icon = glyphs.error
            tint = colors.error

        label = record.label
        timing = _format_timing(record.elapsed_seconds())

        model = record.model or self._model_label
        if not model:
            try:
                if hasattr(self, "app") and self.app is not None:
                    model = getattr(self.app, "_model", None)
            except Exception:
                pass
        if model:
            if ":" in model:
                _, _, m_part = model.partition(":")
                model = m_part or model
            model = _sanitize(model, max_chars=_MODEL_COL)
        else:
            model = ""
        right = self._right_block(model, timing)

        if record.status == "error" and record.error:
            err_text = f" - {record.error}"
            avail = max(10, task_col - len(err_text) - 1)
            task_label = _sanitize(label, max_chars=avail)
            task_str = f"{task_label}{err_text}".ljust(task_col)
            return Content.assemble(
                Content.styled(f"  {icon}  ", tint),
                Content.styled(task_str, colors.foreground),
                Content.styled(right, colors.muted),
            )

        if record.status == "running" and record.current_action:
            action_badge = f"‣ {record.current_action}"
            # Reserve space for the action badge so it remains visible
            max_label_len = max(12, task_col - len(action_badge) - 2)
            if len(label) > max_label_len:
                task_label = label[: max_label_len - 3] + "..."
            else:
                task_label = label
            padding_len = max(1, task_col - len(task_label) - len(action_badge))
            padding = " " * padding_len
            return Content.assemble(
                Content.styled(f"  {icon}  ", tint),
                Content.styled(task_label, colors.foreground),
                Content.styled(padding, colors.foreground),
                Content.styled(action_badge, colors.warning),
                Content.styled(right, colors.muted),
            )

        task = _sanitize(label, max_chars=task_col - 1).ljust(task_col)
        return Content.assemble(
            Content.styled(f"  {icon}  ", tint),
            Content.styled(task, colors.foreground),
            Content.styled(right, colors.muted),
        )

    def _heading_row(self, task_col: int, colors: Any) -> Content:
        prefix = " " * _STATUS_COL
        task = "TASK".ljust(task_col)
        right = self._right_block("MODEL", "TIME")
        return Content.styled(f"{prefix}{task}{right}", colors.muted)


class SubagentColumn(Vertical):
    """Backwards-compatibility column class."""

    def __init__(self, agent_name: str, task: str) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.task_desc = task
