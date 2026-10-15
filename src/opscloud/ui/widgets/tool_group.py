"""Collapsible tool-group summary widget and step-level phrasing helpers.

Extracted from `opscloud.ui.widgets.messages` (Phase 5 UI modularization).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from textual.content import Content
from textual.events import Click
from textual.reactive import var
from textual.widgets import Static

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.tool_call_message import ToolCallMessage
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.timer import Timer
    from textual.widget import Widget

logger = get_logger(__name__)

# Maps a tool name to the summary category it aggregates under. grep/glob share
# "search" so a mixed run folds into a single "Searched for N patterns" segment.
_TOOL_SUMMARY_CATEGORY: dict[str, str] = {
    "read_file": "read",
    "view_file": "read",
    "write_file": "write",
    "write_to_file": "write",
    "edit_file": "edit",
    "replace_file_content": "edit",
    "multi_replace_file_content": "edit",
    "delete": "delete",
    "delete_file": "delete",
    "ls": "ls",
    "list_dir": "ls",
    "grep": "search",
    "glob": "search",
    "execute": "shell",
    "run_command": "shell",
    "run_script": "script",
    "execute_script": "script",
    "web_search": "web_search",
    "fetch_url": "fetch",
    "js_eval": "js",
    "task": "task",
    "subagent": "task",
    "write_todos": "todos",
}

# category -> (present verb, past verb, singular noun, plural noun).
_TOOL_SUMMARY_PHRASES: dict[str, tuple[str, str, str, str]] = {
    "read": ("Reading", "Read", "file", "files"),
    "write": ("Writing", "Wrote", "file", "files"),
    "edit": ("Editing", "Edited", "file", "files"),
    "delete": ("Deleting", "Deleted", "file", "files"),
    "ls": ("Listing", "Listed", "directory", "directories"),
    "search": ("Searching for", "Searched for", "pattern", "patterns"),
    "shell": ("Running", "Ran", "shell command", "shell commands"),
    "script": ("Running", "Ran", "script", "scripts"),
    "js": ("Running", "Ran", "JS evaluation", "JS evaluations"),
    "fetch": ("Fetching", "Fetched", "URL", "URLs"),
    "task": ("Running", "Ran", "agent", "agents"),
}

_Tense = Literal["present", "past"]


def _summary_segment(category: str, count: int, tool_name: str, tense: _Tense) -> str:
    """Phrase a single count segment, e.g. "Read 2 files" / "Reading 2 files"."""
    from opscloud.ui.widgets.tool_display import parse_scoped_tool_name

    scope_tag = ""
    clean_cat = category
    if "[" in category and category.endswith("]"):
        parts = category.split("[", 1)
        clean_cat = parts[0].strip()
        scope_tag = f" [{parts[1]}"

    if clean_cat == "web_search":
        base = "Searching the web" if tense == "present" else "Searched the web"
        res = base if count == 1 else f"{base} {count} times"
        return f"{res}{scope_tag}"
    if clean_cat == "todos":
        res = "Updating todos" if tense == "present" else "Updated todos"
        return f"{res}{scope_tag}"
    phrase = _TOOL_SUMMARY_PHRASES.get(clean_cat)
    if phrase is None:
        base_tool, _ = parse_scoped_tool_name(tool_name)
        present, past = "Running", "Ran"
        singular, plural = f"{base_tool} call", f"{base_tool} calls"
    else:
        present, past, singular, plural = phrase
    verb = present if tense == "present" else past
    noun = singular if count == 1 else plural
    return f"{verb} {count} {noun}{scope_tag}"


def summarize_tool_group(tool_names: list[str], *, tense: _Tense = "past") -> str:
    """Build a one-line summary of a run of tool calls."""
    from opscloud.ui.widgets.tool_display import parse_scoped_tool_name

    counts: dict[str, int] = {}
    order: list[str] = []
    rep_name: dict[str, str] = {}
    for name in tool_names:
        base_name, scope = parse_scoped_tool_name(name)
        base_cat = _TOOL_SUMMARY_CATEGORY.get(
            base_name, _TOOL_SUMMARY_CATEGORY.get(name, base_name)
        )
        category = f"{base_cat} [{scope}]" if scope else base_cat
        if category not in counts:
            counts[category] = 0
            order.append(category)
            rep_name[category] = name
        counts[category] += 1

    segments = [
        _summary_segment(cat, counts[cat], rep_name[cat], tense) for cat in order
    ]
    if not segments:
        return "Running tools" if tense == "present" else "Ran tools"
    return _join_segments(segments)


def _join_segments(segments: list[str]) -> str:
    """Join summary segments, lowercasing the lead word of all but the first."""
    first, *rest = segments
    lowered = [f"{seg[0].lower()}{seg[1:]}" if seg else seg for seg in rest]
    return ", ".join([first, *lowered])


def summarize_live_tool_group(
    completed_names: list[str], pending_names: list[str]
) -> str:
    """Summarize an in-flight run, mixing past and present tense."""
    segments: list[str] = []
    if completed_names:
        segments.append(summarize_tool_group(completed_names, tense="past"))
    if pending_names:
        segments.append(summarize_tool_group(pending_names, tense="present"))
    if not segments:
        return ""
    return _join_segments(segments)


_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS = "-tool-group-collapsed-accessory"
"""Marker class hiding a collapsed group's accessory widgets."""


class ToolGroupSummary(Static):
    """Collapsed one-line stand-in for an assistant step's tool calls."""

    DEFAULT_CSS = """
    ToolGroupSummary {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        color: $text-muted;
        pointer: pointer;
    }

    ToolGroupSummary:hover {
        color: $text;
    }
    """

    _SPINNER_INTERVAL: ClassVar[float] = 0.1

    _collapsed: var[bool] = var(True)

    def __init__(
        self,
        tools: list[ToolCallMessage] | None = None,
        collapsible: list[Widget] | None = None,
        *,
        accessories: dict[Widget, list[Widget]] | None = None,
        live: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__("", **kwargs)
        self._tools = list(tools or [])
        self._collapsible = list(collapsible or [])
        self._accessories: dict[Widget, list[Widget]] = {}
        for owner, widgets in (accessories or {}).items():
            self._attach_accessories(owner, widgets)
        self._accepting_members = live
        self._finalized = not live
        self._spinner_pos = 0
        self._timer: Timer | None = None
        self._present_text: str | None = None
        self._past_text: str | None = None
        self._present_key: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def on_mount(self) -> None:
        """Apply initial visibility, render, and arm the spinner if live."""
        self._apply_visibility()
        self._render_line()
        self._sync_timer()

    def _attach_accessories(self, owner: Widget, accessories: Iterable[Widget]) -> None:
        """Record `owner`'s accessories and link them to its approval hiding."""
        linked = [a for a in accessories if a not in self._accessories.get(owner, ())]
        if not linked:
            return
        self._accessories.setdefault(owner, []).extend(linked)
        if isinstance(owner, ToolCallMessage):
            owner._register_visibility_accessories(*linked)

    def add_member(self, tool: ToolCallMessage, *accessories: Widget) -> None:
        """Add a tool to a live group and link its accessories."""
        tool.add_class("-grouped")
        self._tools.append(tool)
        self._collapsible.append(tool)
        self._attach_accessories(tool, accessories)
        self._present_text = self._past_text = self._present_key = None
        self._apply_visibility()
        in_progress = self._sync_lifecycle()
        self._render_line(in_progress=in_progress)

    def add_collapsible(self, widget: Widget, *accessories: Widget) -> None:
        """Attach a non-tool widget (e.g. a diff) and its accessories."""
        widget.add_class("-grouped")
        self._collapsible.append(widget)
        self._attach_accessories(widget, accessories)
        self._apply_collapsible_visibility(widget, visible=not self._collapsed)

    def close(self) -> None:
        """Stop accepting members and finalize after every tool settles."""
        self._accepting_members = False
        self._evict_failed()
        in_progress = self._sync_lifecycle()
        if not self.is_attached:
            return
        if self._tools:
            self._render_line(in_progress=in_progress)
        else:
            self._release_all_collapsible()
            self.remove()

    def _release_collapsible(self, widget: Widget) -> None:
        """Drop a widget's group linkage and clear its collapsed accessory class."""
        if widget in self._collapsible:
            self._collapsible.remove(widget)
        widget.remove_class("-grouped")
        for accessory in self._accessories.pop(widget, []):
            accessory.remove_class(_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS)

    def _release_all_collapsible(self) -> None:
        """Release and reveal every remaining folded widget."""
        for widget in list(self._collapsible):
            self._release_collapsible(widget)
            if widget.is_attached:
                widget.display = True

    def reveal_pending(self) -> None:
        """Remove unfinished tool calls from the collapsed group."""
        pending = [tool for tool in self._tools if tool.is_pending]
        if not pending:
            return
        for tool in pending:
            self._tools.remove(tool)
            self._release_collapsible(tool)
            if tool.is_attached and not tool._awaiting_approval:
                tool.display = True
        self._present_text = self._past_text = self._present_key = None
        in_progress = self._sync_lifecycle()
        if self._tools:
            self._render_line(in_progress=in_progress)
            return
        self._release_all_collapsible()
        if self.is_attached:
            self.remove()

    @property
    def has_attached_members(self) -> bool:
        """Whether any collapsed widget is still attached to the DOM."""
        return any(widget.is_attached for widget in self._collapsible)

    def toggle(self) -> None:
        """Toggle between collapsed and expanded."""
        self._collapsed = not self._collapsed

    def watch__collapsed(self, _collapsed: bool) -> None:
        """Re-render and re-apply member visibility when the state changes."""
        if not self.is_attached:
            self._apply_visibility()
            self._render_line()
            return
        with self.app.batch_update():
            self._apply_visibility()
            self._render_line()

    def on_click(self, event: Click) -> None:
        """Toggle the group on click."""
        event.stop()
        self.toggle()

    def _in_progress(self) -> bool:
        """Whether any member tool is still pending or running."""
        return any(tool.is_pending for tool in self._tools)

    def _sync_lifecycle(self, *, in_progress: bool | None = None) -> bool:
        """Finalize only once a closed group's retained tools have settled."""
        if in_progress is None:
            in_progress = self._in_progress()
        self._finalized = not self._accepting_members and not in_progress
        self._sync_timer()
        return in_progress

    def _evict_failed(self) -> None:
        """Un-fold errored/rejected/skipped tools so non-successes stay visible."""
        failed = [t for t in self._tools if t.is_failed]
        if not failed:
            return
        for tool in failed:
            self._tools.remove(tool)
            self._release_collapsible(tool)
            if tool.is_attached:
                tool.display = True
        self._present_text = self._past_text = self._present_key = None

    def _sync_timer(self) -> None:
        """Run the spinner timer only while live members are in progress."""
        if not self._finalized and self._in_progress():
            if self._timer is None:
                self._timer = self.set_interval(self._SPINNER_INTERVAL, self._tick)
        else:
            self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def sync_members(self) -> None:
        """Synchronize member status, evict failures, and update the summary line."""
        before = len(self._tools)
        self._evict_failed()
        evicted = len(self._tools) != before
        if self._collapsed:
            self._apply_visibility()
        if not self._tools:
            self._sync_lifecycle(in_progress=False)
            self._release_all_collapsible()
            if self.is_attached:
                self.remove()
            return
        in_progress = self._sync_lifecycle()
        self._render_line(
            in_progress=in_progress, layout=evicted or not in_progress
        )

    def _tick(self) -> None:
        """Advance the spinner, eject failures, and flip to past tense when done."""
        try:
            self._spinner_pos += 1
            self.sync_members()
        except Exception:
            logger.exception("ToolGroupSummary spinner tick failed; stopping timer")
            self._stop_timer()

    def _apply_collapsible_visibility(self, widget: Widget, *, visible: bool) -> None:
        """Apply the group's visibility to a widget and its accessories."""
        if widget.is_attached and widget.display != visible:
            widget.display = visible
        for accessory in self._accessories.get(widget, []):
            accessory.set_class(not visible, _TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS)

    def _apply_visibility(self) -> None:
        """Show or hide every folded widget, and its accessories, per collapse."""
        visible = not self._collapsed
        for widget in self._collapsible:
            self._apply_collapsible_visibility(widget, visible=visible)

    def _render_line(
        self, *, in_progress: bool | None = None, layout: bool = True
    ) -> None:
        """Refresh the summary line for the current tense and collapsed state."""
        if not self.is_attached:
            return
        if not self._tools:
            self.update(Content(""), layout=layout)
            return
        glyphs = get_glyphs()
        if in_progress is None:
            in_progress = self._in_progress()
        if not self._finalized and in_progress:
            pending = [tool.tool_name for tool in self._tools if tool.is_pending]
            completed = [tool.tool_name for tool in self._tools if not tool.is_pending]
            key = (tuple(completed), tuple(pending))
            summary_changed = self._present_text is None or key != self._present_key
            if summary_changed:
                self._present_text = summarize_live_tool_group(completed, pending)
                self._present_key = key
            frames = glyphs.spinner_frames
            spinner = frames[self._spinner_pos % len(frames)]
            self.update(
                self._live_line(spinner, completed, pending),
                layout=layout or summary_changed,
            )
        else:
            mark = (
                glyphs.disclosure_collapsed
                if self._collapsed
                else glyphs.disclosure_expanded
            )
            if self._past_text is None:
                self._past_text = summarize_tool_group(
                    [tool.tool_name for tool in self._tools], tense="past"
                )
            self.update(
                Content.assemble(
                    (f"{mark} ", "$primary"), *_emphasize_counts(self._past_text)
                ),
                layout=layout,
            )

    @staticmethod
    def _live_line(spinner: str, completed: list[str], pending: list[str]) -> Content:
        """Live line: finished work muted, the in-flight segment in teal."""
        ellipsis = get_glyphs().ellipsis
        pieces: list[tuple[str, str] | str] = [(f"{spinner} ", "$primary")]
        done = summarize_tool_group(completed, tense="past") if completed else ""
        active = summarize_tool_group(pending, tense="present") if pending else ""
        if done:
            pieces.extend(_emphasize_counts(done))
        if active:
            if done:
                pieces.append(", ")
                active = f"{active[0].lower()}{active[1:]}"
            pieces.append((f"{active}{ellipsis}", "$primary"))
        return Content.assemble(*pieces)


_COUNT_RE = re.compile(r"\b\d+\b")


def _emphasize_counts(text: str) -> list[tuple[str, str] | str]:
    """Split ``text`` so bare numbers render in teal ("Read **2** files")."""
    pieces: list[tuple[str, str] | str] = []
    last = 0
    for match in _COUNT_RE.finditer(text):
        if match.start() > last:
            pieces.append(text[last : match.start()])
        pieces.append((match.group(), "$primary"))
        last = match.end()
    if last < len(text):
        pieces.append(text[last:])
    return pieces
