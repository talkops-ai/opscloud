"""Tests for the autocomplete card: match highlighting, aligned rows, selection."""

from __future__ import annotations

import importlib

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.content import Content

from opscloud.ui.widgets.chat_input import (
    ChatInput,
    CompletionOption,
    CompletionPopup,
    _match_spans,
    completion_kind,
    completion_label_width,
    format_completion_row,
)

# `opscloud.config.settings` is shadowed by a Settings instance on the package.
settings = importlib.import_module("opscloud.config.settings")


class _ThemedApp(App):
    """Minimal app carrying the OpsCloud theme variables ChatInput's CSS uses."""

    def __init__(self) -> None:
        super().__init__()
        from opscloud.ui.theme import register_app_themes

        register_app_themes(self)
        self.theme = "opscloud-dark"

    def get_theme_variable_defaults(self) -> dict[str, str]:
        from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors

        return get_css_variable_defaults(colors=get_theme_colors(self))

    def compose(self) -> ComposeResult:
        yield ChatInput(id="ci")


@pytest.fixture
def unicode_glyphs(monkeypatch):
    """Pin the Unicode glyph set so assertions don't depend on the terminal."""
    monkeypatch.setattr(settings, "_glyphs_cache", settings.UNICODE_GLYPHS)


@pytest.fixture
def ascii_glyphs(monkeypatch):
    monkeypatch.setattr(settings, "_glyphs_cache", settings.ASCII_GLYPHS)


def _styled(content: Content) -> dict[str, str]:
    """Map each styled run's visible text to its style string."""
    return {content.plain[s.start : s.end]: str(s.style) for s in content.spans}


# ── _match_spans ────────────────────────────────────────


def test_match_prefix():
    assert _match_spans("pl", "plugins") == [0, 1]


def test_match_prefers_contiguous_run():
    # "pl" appears contiguously late in the word; prefer it over p…l.
    assert _match_spans("pl", "apple") == [2, 3]


def test_match_subsequence():
    assert _match_spans("pg", "plugins") == [0, 3]


def test_match_falls_back_when_run_strands_a_char():
    # Run-first takes the "ab" at 4 and strands "c"; a…b…c still matches.
    assert _match_spans("abc", "axbcab") == [0, 2, 3]


def test_match_is_case_insensitive():
    assert _match_spans("PL", "Plugins") == [0, 1]


def test_no_match_or_empty_query():
    assert _match_spans("zz", "plugins") == []
    assert _match_spans("", "plugins") == []


# ── completion_label_width ──────────────────────────────


def test_label_width_is_longest_label():
    rows = [("/plan", "a"), ("/plugins", "b"), ("/pool", "c")]
    assert completion_label_width(rows, kind="command", width=100) == len("plugins")


def test_label_width_counts_argument_hint():
    rows = [("/model", "a"), ("/plugins", "b")]
    width = completion_label_width(rows, ["<name>", ""], kind="command", width=100)
    assert width == len("model <name>")


def test_label_width_ignores_hints_when_descriptions_would_starve():
    rows = [("/workflow", "a"), ("/plugins", "b")]
    hints = ["[<name> [params]]", ""]
    # 52 - 2 prefix - 22 column - 2 gap = 26 >= 24: hints still count.
    assert completion_label_width(rows, hints, kind="command", width=52) == 22
    # 48 - 2 - 22 - 2 = 22 < 24: size on names only.
    assert completion_label_width(rows, hints, kind="command", width=48) == len("workflow")


def test_label_width_caps_commands_at_22():
    rows = [("/" + "x" * 40, "desc")]
    assert completion_label_width(rows, kind="command", width=200) == 22


def test_label_width_lets_files_use_free_space():
    path = "@src/opscloud/ui/widgets/chat_input.py"
    width = completion_label_width([(path, "py")], kind="file", width=100)
    assert width == len(path) - 1  # "@" dropped, no cap at 22


def test_completion_kind():
    assert completion_kind([("@src/app.py", "py")]) == "file"
    assert completion_kind([("/plugins", "x")]) == "command"


# ── format_completion_row ───────────────────────────────


def test_rows_align_descriptions(unicode_glyphs):
    rows = [("/plan", "Plan it"), ("/plugins", "Manage plugins")]
    column = completion_label_width(rows, kind="command", width=80)
    plains = [
        format_completion_row(
            label, desc, query="", label_width=column, width=80, selected=False
        ).plain
        for label, desc in rows
    ]
    assert plains[0].index("Plan it") == plains[1].index("Manage plugins")
    assert plains[0] == "  plan     Plan it"


def test_long_label_gets_ellipsis(unicode_glyphs):
    content = format_completion_row(
        "/" + "a" * 30, "desc", query="", label_width=22, width=80, selected=False
    )
    label_part = content.plain[2 : 2 + 22]
    assert label_part.endswith("…")
    assert cell_len(label_part) == 22


def test_long_description_gets_ellipsis_and_fits_width(unicode_glyphs):
    content = format_completion_row(
        "/plugins", "word " * 40, query="", label_width=7, width=40, selected=False
    )
    assert "\n" not in content.plain
    assert cell_len(content.plain) == 40
    assert content.plain.endswith("…")


def test_highlight_spans_are_bold_primary(unicode_glyphs):
    content = format_completion_row(
        "/plugins", "Manage", query="pl", label_width=7, width=60, selected=False
    )
    assert _styled(content)["pl"] == "bold $primary"


def test_selected_row_starts_with_accent_bar(unicode_glyphs):
    selected = format_completion_row(
        "/plugins", "Manage", query="", label_width=7, width=60, selected=True
    )
    plain_row = format_completion_row(
        "/plugins", "Manage", query="", label_width=7, width=60, selected=False
    )
    assert selected.plain.startswith("▌ plugins")
    assert _styled(selected)["▌"] == "$primary"
    assert _styled(selected)["plugins"] == "bold"
    assert plain_row.plain.startswith("  plugins")


def test_selected_row_uses_ascii_bar(ascii_glyphs):
    content = format_completion_row(
        "/plugins", "Manage", query="", label_width=7, width=60, selected=True
    )
    assert content.plain.startswith("> plugins")


def test_file_row_dims_directory(unicode_glyphs):
    content = format_completion_row(
        "@src/opscloud/ui/app.py",
        "py",
        query="",
        label_width=22,
        width=60,
        selected=False,
        kind="file",
    )
    styled = _styled(content)
    assert content.plain.startswith("  src/opscloud/ui/app.py")
    assert styled["src/opscloud/ui/"] == "dim"
    assert "app.py" not in styled  # basename stays at normal weight


def test_file_row_highlights_match_over_dim(unicode_glyphs):
    content = format_completion_row(
        "@src/opscloud/ui/app.py",
        "py",
        query="ui/app",
        label_width=22,
        width=60,
        selected=False,
        kind="file",
    )
    assert _styled(content)["ui/app"] == "bold $primary"


def test_long_file_path_keeps_basename(unicode_glyphs):
    content = format_completion_row(
        "@src/opscloud/ui/widgets/chat_input.py",
        "py",
        query="",
        label_width=20,
        width=60,
        selected=False,
        kind="file",
    )
    label_part = content.plain[2 : 2 + 20]
    assert label_part.startswith("…")
    assert label_part.endswith("chat_input.py")


def test_argument_hint_is_dim_after_label(unicode_glyphs):
    content = format_completion_row(
        "/model",
        "Switch model",
        query="",
        label_width=12,
        width=60,
        selected=False,
        argument_hint="<name>",
    )
    assert content.plain.startswith("  model <name>  Switch model")
    assert _styled(content)[" <name>"] == "dim"


# ── Mounted behaviour ───────────────────────────────────


async def _type_slash_pl(pilot, ci: ChatInput) -> CompletionPopup:
    ci.focus()
    await pilot.press("/", "p", "l")
    await pilot.pause()
    await pilot.pause()
    return ci._autocomplete


@pytest.mark.asyncio
async def test_typing_slash_query_shows_card_with_first_selected():
    app = _ThemedApp()
    async with app.run_test(size=(110, 20)) as pilot:
        ci = app.query_one(ChatInput)
        popup = await _type_slash_pl(pilot, ci)
        assert popup.styles.display == "block"
        options = list(popup.query(CompletionOption))
        assert options
        assert options[0].has_class("completion-option-selected")
        assert not any(o.has_class("completion-option-selected") for o in options[1:])


@pytest.mark.asyncio
async def test_down_arrow_moves_selection():
    app = _ThemedApp()
    async with app.run_test(size=(110, 20)) as pilot:
        ci = app.query_one(ChatInput)
        popup = await _type_slash_pl(pilot, ci)
        await pilot.press("down")
        await pilot.pause()
        await pilot.pause()
        options = list(popup.query(CompletionOption))
        assert len(options) > 1
        assert not options[0].has_class("completion-option-selected")
        assert options[1].has_class("completion-option-selected")


@pytest.mark.asyncio
async def test_rows_are_one_line_at_narrow_width():
    app = _ThemedApp()
    async with app.run_test(size=(56, 20)) as pilot:
        ci = app.query_one(ChatInput)
        popup = await _type_slash_pl(pilot, ci)
        for option in popup.query(CompletionOption):
            assert option.size.height == 1
            rendered = option.render()
            assert isinstance(rendered, Content)
            assert cell_len(rendered.plain) <= option.size.width
