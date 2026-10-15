"""Enhanced diff widget for displaying unified diffs with line numbers and CSS-driven background highlights."""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from opscloud.ui import theme
from opscloud.config.settings import get_glyphs, is_ascii_mode

if TYPE_CHECKING:
    from textual.app import ComposeResult

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)")
"""Matches a unified-diff hunk header, capturing the old and new start lines."""

_HIDDEN_HUNK_RE = re.compile(r"^@@ \d+ lines? hidden @@$")
"""Matches a folded context separator line."""

DEFAULT_DIFF_PREVIEW_LINES = 33
"""Default number of diff lines shown inline before truncation."""

DEFAULT_DIFF_LINE_WIDTH = 104
"""Default character width for full-row diff background tint padding."""

_DIFF_ADD_FG = "#86efac"
_DIFF_ADD_SIGN = "#4ade80"
_DIFF_ADD_BG = "#0e340f"

_DIFF_DEL_FG = "#fca5a5"
_DIFF_DEL_SIGN = "#f87171"
_DIFF_DEL_BG = "#350d0e"


def compute_line_diff(
    old_string: str,
    new_string: str,
    start_line: int | None = None,
    context_lines: int = 3,
) -> list[str]:
    """Compute unified diff lines with context folding and accurate line numbers.

    Uses line-level sequence matching so unchanged lines within a replacement
    block render as context (or fold into ``@@ N lines hidden @@``) rather than
    appearing as wholesale deletions and additions.

    Args:
        old_string: Original text before the edit.
        new_string: Replacement text after the edit.
        start_line: 1-based starting line number in the target file.
        context_lines: Number of unchanged context lines to keep around changes.

    Returns:
        Unified diff lines including ``@@ -old,len +new,len @@`` hunk headers.
    """
    if not old_string and not new_string:
        return []

    old_lines = old_string.splitlines() if old_string else []
    new_lines = new_string.splitlines() if new_string else []
    base_line = start_line if (start_line is not None and start_line > 0) else 1

    if not old_lines and new_lines:
        return [f"@@ -{base_line},0 +{base_line},{len(new_lines)} @@"] + [
            f"+{line}" for line in new_lines
        ]
    if old_lines and not new_lines:
        return [f"@@ -{base_line},{len(old_lines)} +{base_line},0 @@"] + [
            f"-{line}" for line in old_lines
        ]

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    groups = list(matcher.get_grouped_opcodes(n=context_lines))
    if not groups:
        return []

    diff_lines: list[str] = []
    prev_i2 = 0

    for group_idx, group in enumerate(groups):
        first_i1 = group[0][1]
        last_i2 = group[-1][2]
        first_j1 = group[0][3]
        last_j2 = group[-1][4]

        if group_idx == 0 and first_i1 > 0:
            diff_lines.append(f"@@ {first_i1} lines hidden @@")
        elif group_idx > 0 and first_i1 > prev_i2:
            hidden = first_i1 - prev_i2
            diff_lines.append(f"@@ {hidden} lines hidden @@")

        old_start = base_line + first_i1
        old_count = last_i2 - first_i1
        new_start = base_line + first_j1
        new_count = last_j2 - first_j1
        diff_lines.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@")

        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    diff_lines.append(f" {line}")
            else:
                if tag in ("replace", "delete"):
                    for line in old_lines[i1:i2]:
                        diff_lines.append(f"-{line}")
                if tag in ("replace", "insert"):
                    for line in new_lines[j1:j2]:
                        diff_lines.append(f"+{line}")

        prev_i2 = last_i2

    trailing_hidden = len(old_lines) - prev_i2
    if trailing_hidden > 0 and len(groups) > 0 and prev_i2 > 0 and trailing_hidden > context_lines:
        diff_lines.append(f"@@ {trailing_hidden} lines hidden @@")

    return diff_lines


def format_diff_stats(additions: int, deletions: int) -> Content:
    """Format ``+N -M`` addition and deletion counts as styled Content."""
    parts: list[str | tuple[str, str] | Content] = []
    if additions > 0 or (additions == 0 and deletions > 0):
        parts.append((f"+{additions}", f"bold {_DIFF_ADD_SIGN}"))
    if deletions > 0 or (deletions == 0 and additions > 0):
        if parts:
            parts.append(" ")
        parts.append((f"-{deletions}", f"bold {_DIFF_DEL_SIGN}"))
    return Content.assemble(*parts) if parts else Content("")


def format_file_diff_header(
    file_path: str,
    additions: int = 0,
    deletions: int = 0,
) -> Content:
    """Format ``<bold file_path>  +<additions> -<deletions>`` header line."""
    stats = format_diff_stats(additions, deletions)
    if stats.plain:
        return Content.assemble(
            (file_path, "bold"),
            "  ",
            stats,
        )
    return Content.styled(file_path, "bold")


def _parse_display_entries(
    diff: str,
) -> tuple[list[tuple[str, int | None, str]], int, int, int]:
    """Parse unified diff text into displayable entries and summary stats.

    Returns:
        Tuple of ``(entries, additions, deletions, max_line_num)`` where each
        entry is ``(kind, line_num, text)`` with ``kind`` in
        ``{"add", "del", "ctx", "hidden", "other"}``.
    """
    lines = diff.splitlines()
    entries: list[tuple[str, int | None, str]] = []
    additions = 0
    deletions = 0
    old_num = 1
    new_num = 1
    max_line = 1

    for line in lines:
        if line.startswith(("---", "+++")):
            continue
        if _HIDDEN_HUNK_RE.match(line):
            entries.append(("hidden", None, line))
            continue
        if m := _HUNK_RE.match(line):
            old_num, new_num = int(m.group(1)), int(m.group(2))
            max_line = max(max_line, old_num, new_num)
            continue

        raw_content = (line[1:] if line else "").expandtabs(4)
        if line.startswith("-"):
            deletions += 1
            max_line = max(max_line, old_num)
            entries.append(("del", old_num, raw_content))
            old_num += 1
        elif line.startswith("+"):
            additions += 1
            max_line = max(max_line, new_num)
            entries.append(("add", new_num, raw_content))
            new_num += 1
        elif line.startswith(" "):
            max_line = max(max_line, new_num)
            entries.append(("ctx", new_num, raw_content))
            old_num += 1
            new_num += 1
        elif line.strip() in ("...", "…"):
            entries.append(("hidden", None, line.strip()))
        else:
            entries.append(("other", None, line.expandtabs(4)))

    return entries, additions, deletions, max_line


def _format_diff_entry_content(
    kind: str,
    line_num: int | None,
    content: str,
    gutter_width: int,
    pad_width: int,
) -> Content:
    """Format a single diff row with an untinted line-number gutter and full-width tinted code body."""
    if kind == "add":
        num_str = f"{line_num:>{gutter_width}} " if line_num is not None else " " * (gutter_width + 1)
        body_text = f"+ {content}"
        padded_body = body_text.ljust(pad_width)
        return Content.assemble(
            (num_str, "dim"),
            (padded_body[:2], f"bold {_DIFF_ADD_SIGN} on {_DIFF_ADD_BG}"),
            (padded_body[2:], f"{_DIFF_ADD_FG} on {_DIFF_ADD_BG}"),
        )
    if kind == "del":
        num_str = f"{line_num:>{gutter_width}} " if line_num is not None else " " * (gutter_width + 1)
        body_text = f"- {content}"
        padded_body = body_text.ljust(pad_width)
        return Content.assemble(
            (num_str, "dim"),
            (padded_body[:2], f"bold {_DIFF_DEL_SIGN} on {_DIFF_DEL_BG}"),
            (padded_body[2:], f"{_DIFF_DEL_FG} on {_DIFF_DEL_BG}"),
        )
    if kind == "ctx":
        num_str = f"{line_num:>{gutter_width}} " if line_num is not None else " " * (gutter_width + 1)
        return Content.assemble(
            (num_str, "dim"),
            (f"  {content}", "dim"),
        )
    if kind == "hidden":
        pad_gutter = " " * (gutter_width + 1)
        return Content.styled(f"{pad_gutter}{content}", "dim")
    return Content.styled(content, "dim")


def render_diff_content(
    diff: str,
    max_lines: int | None = DEFAULT_DIFF_PREVIEW_LINES,
    show_stats: bool = False,
    expand_hint: str = "f for full diff",
    line_width: int = DEFAULT_DIFF_LINE_WIDTH,
) -> tuple[Content, int]:
    """Render a unified diff into a single styled ``Content`` block and remaining line count.

    Args:
        diff: Unified diff string.
        max_lines: Maximum number of diff lines to show (``None`` for unlimited).
        show_stats: Whether to prepend the ``+N -M`` stats line.
        expand_hint: Keybinding/interaction hint shown when lines are truncated.
        line_width: Target width for full-row background tint padding.

    Returns:
        Tuple of ``(rendered_content, hidden_line_count)``.
    """
    if not diff or not diff.strip():
        return Content.styled("No changes detected", "dim"), 0

    entries, additions, deletions, max_line = _parse_display_entries(diff)
    if not entries:
        return Content.styled("No changes detected", "dim"), 0

    gutter_width = max(2, len(str(max_line)))
    max_content_len = max((len(text) + 4 for _, _, text in entries), default=line_width)
    pad_width = max(line_width, min(max_content_len, 140))

    rows: list[Content] = []
    if show_stats and (additions or deletions):
        rows.append(format_diff_stats(additions, deletions))

    shown_entries = entries[:max_lines] if (max_lines is not None and max_lines > 0) else entries
    remaining = max(0, len(entries) - len(shown_entries))

    for kind, line_num, text in shown_entries:
        rows.append(_format_diff_entry_content(kind, line_num, text, gutter_width, pad_width))

    if remaining > 0 and expand_hint:
        ellipsis = get_glyphs().ellipsis
        noun = "line" if remaining == 1 else "lines"
        rows.append(Content.styled(f"{ellipsis} {remaining} more {noun} ({expand_hint})", "dim"))

    return Content("\n").join(rows), remaining


def compose_diff_lines(
    diff: str,
    max_lines: int | None = DEFAULT_DIFF_PREVIEW_LINES,
    show_stats: bool = True,
    *,
    expand_hint: str = "f for full diff",
    line_width: int = DEFAULT_DIFF_LINE_WIDTH,
) -> ComposeResult:
    """Yield per-line Static widgets for a unified diff.

    Each line features a right-aligned dim line-number gutter on the default
    terminal background, followed by a ``+``/``-``/`` `` prefixed code block
    whose green/red background tint spans the full row width.

    Args:
        diff: Unified diff string.
        max_lines: Maximum number of diff lines to show (None for unlimited).
        show_stats: Whether to render the ``+X -Y`` stats header widget.
        expand_hint: Keybinding hint displayed in the truncation footer.
        line_width: Target width for full-row background tint padding.

    Yields:
        Static widgets — one per diff line — with appropriate CSS classes.
    """
    if not diff or not diff.strip():
        yield Static(Content.styled("No changes detected", "dim"))
        return

    entries, additions, deletions, max_line = _parse_display_entries(diff)
    if not entries:
        yield Static(Content.styled("No changes detected", "dim"))
        return

    if show_stats and (additions or deletions):
        yield Static(format_diff_stats(additions, deletions), classes="diff-stats-header")

    gutter_width = max(2, len(str(max_line)))
    max_content_len = max((len(text) + 4 for _, _, text in entries), default=line_width)
    pad_width = max(line_width, min(max_content_len, 140))

    shown_entries = entries[:max_lines] if (max_lines is not None and max_lines > 0) else entries
    remaining = max(0, len(entries) - len(shown_entries))

    for kind, line_num, text in shown_entries:
        row_content = _format_diff_entry_content(kind, line_num, text, gutter_width, pad_width)
        if kind == "del":
            yield Static(row_content, classes="diff-line-removed")
        elif kind == "add":
            yield Static(row_content, classes="diff-line-added")
        elif kind == "ctx":
            yield Static(row_content, classes="diff-line-context")
        else:
            yield Static(row_content, classes="diff-line-hunk")

    if remaining > 0:
        ellipsis = get_glyphs().ellipsis
        noun = "line" if remaining == 1 else "lines"
        hint_suffix = f" ({expand_hint})" if expand_hint else ""
        yield Static(
            Content.styled(f"{ellipsis} {remaining} more {noun}{hint_suffix}", "dim"),
            classes="diff-line-truncated",
        )


class EnhancedDiff(Vertical):
    """Widget for displaying a unified diff with syntax highlighting."""

    DEFAULT_CSS = """
    EnhancedDiff {
        height: auto;
        padding: 1;
        background: $surface;
        border: round $primary;
    }

    EnhancedDiff .diff-title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }

    EnhancedDiff .diff-content {
        height: auto;
    }

    EnhancedDiff .diff-stats {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        diff: str,
        title: str = "Diff",
        max_lines: int | None = 100,
        **kwargs: Any,
    ) -> None:
        """Initialize the diff widget."""
        super().__init__(**kwargs)
        self._diff = diff
        self._title = title
        self._max_lines = max_lines
        self._stats = self._compute_stats()

    def _compute_stats(self) -> tuple[int, int]:
        """Compute additions and deletions count."""
        additions = 0
        deletions = 0
        for line in self._diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        return additions, deletions

    def on_mount(self) -> None:
        """Set border style based on charset mode."""
        if is_ascii_mode():
            colors = theme.get_theme_colors(self)
            self.styles.border = ("ascii", colors.primary)

    def compose(self) -> ComposeResult:
        """Compose the diff widget layout."""
        colors = theme.get_theme_colors(self)
        glyphs = get_glyphs()
        h = glyphs.box_double_horizontal
        yield Static(
            Content.styled(
                f"{h}{h}{h} {self._title} {h}{h}{h}", f"bold {colors.primary}"
            ),
            classes="diff-title",
        )

        yield from compose_diff_lines(self._diff, self._max_lines)

        additions, deletions = self._stats
        if additions or deletions:
            content_parts: list[str | tuple[str, str] | Content] = []
            if additions:
                content_parts.append((f"+{additions}", colors.success))
            if deletions:
                if content_parts:
                    content_parts.append(" ")
                content_parts.append((f"-{deletions}", colors.error))
            yield Static(Content.assemble(*content_parts), classes="diff-stats")
