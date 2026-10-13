"""Tests for the status bar segments, context meter and PROD pill."""

from __future__ import annotations

from textual.content import Content
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from opscloud.ui import context_detection
from opscloud.ui.context_detection import (
    current_k8s_context,
    detect_production,
    is_production_name,
)
from opscloud.ui.widgets.status import (
    StatusBar,
    context_percent_style,
    format_context_meter,
    format_cwd_short,
)


class _ThemedApp(App):
    """Minimal app carrying the OpsCloud theme variables StatusBar's CSS uses."""

    def __init__(self) -> None:
        super().__init__()
        from opscloud.ui.theme import register_app_themes

        register_app_themes(self)
        self.theme = "opscloud-dark"

    def get_theme_variable_defaults(self) -> dict[str, str]:
        from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors

        return get_css_variable_defaults(colors=get_theme_colors(self))

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")


@pytest.fixture(autouse=True)
def _no_real_kube_context(monkeypatch):
    """Never let the developer's kube context leak into status-bar tests."""
    monkeypatch.setattr(context_detection, "current_k8s_context", lambda: "")
    monkeypatch.delenv("TF_WORKSPACE", raising=False)


def _styled(content) -> dict[str, str]:
    return {content.plain[s.start : s.end]: str(s.style) for s in content.spans}


# ── is_production_name / detect_production ─────────────


@pytest.mark.parametrize(
    "name",
    ["client-prod", "prod", "acme_production", "prd-eu", "PROD", "eks:prod/api", "team@prod"],
)
def test_production_names(name):
    assert is_production_name(name)


@pytest.mark.parametrize(
    "name",
    ["product-dev", "reproduce", "preprod-test", "preprod", "client-devops", "", None],
)
def test_non_production_names(name):
    assert not is_production_name(name)


def test_detect_production_reports_source_in_priority_order():
    assert detect_production(aws_profile="client-prod") == "aws"
    assert detect_production(aws_profile="dev", k8s_context="eks-prod") == "k8s"
    assert detect_production(tf_workspace="production") == "tf"
    assert detect_production(aws_profile="prod", k8s_context="prod") == "aws"
    assert detect_production(aws_profile="dev", k8s_context="minikube") is None


def test_current_k8s_context_reads_kubeconfig(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text(
        "apiVersion: v1\ncontexts: []\ncurrent-context: \"eks-prod\"\nkind: Config\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KUBECONFIG_CONTEXT", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(cfg))
    assert current_k8s_context() == "eks-prod"


def test_current_k8s_context_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG_CONTEXT", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "nope"))
    assert current_k8s_context() == ""


def test_current_k8s_context_env_override(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_CONTEXT", "kind-local")
    assert current_k8s_context() == "kind-local"


# ── format_context_meter ────────────────────────────────


@pytest.mark.parametrize(
    ("percent", "plain", "style"),
    [
        (0, "▱▱▱▱▱ 0%", "dim"),
        (20, "▰▱▱▱▱ 20%", "dim"),
        (61, "▰▰▰▰▱ 61%", "$warning"),
        (81, "▰▰▰▰▰ 81%", "$error"),
        (100, "▰▰▰▰▰ 100%", "$error"),
    ],
)
def test_context_meter(percent, plain, style):
    content = format_context_meter(percent, ascii=False)
    assert content.plain == plain
    assert _styled(content)[f" {percent}%"] == style


def test_context_meter_any_usage_fills_one_cell():
    assert format_context_meter(2, ascii=False).plain == "▰▱▱▱▱ 2%"


def test_context_meter_ascii():
    assert format_context_meter(61, ascii=True).plain == "####- 61%"


def test_context_meter_clamps():
    assert format_context_meter(150, ascii=False).plain == "▰▰▰▰▰ 100%"


def test_percent_style_thresholds_are_exclusive():
    assert context_percent_style(60) == "dim"
    assert context_percent_style(60.1) == "$warning"
    assert context_percent_style(80) == "$warning"
    assert context_percent_style(80.1) == "$error"


def test_format_cwd_short():
    assert format_cwd_short("~/Documents/work/opscloud").endswith("/opscloud")
    assert format_cwd_short("~/Documents/work/opscloud").startswith("~/")
    assert "work" not in format_cwd_short("~/Documents/work/opscloud")
    assert format_cwd_short("~/work/opscloud") == "~/work/opscloud"
    assert format_cwd_short("/tmp") == "/tmp"


# ── Mounted StatusBar ───────────────────────────────────


async def _bar(pilot) -> StatusBar:
    bar = pilot.app.query_one(StatusBar)
    bar.set_model(provider="google", model="gemini-3.8-flash")
    bar.set_context_limit(1_000_000)
    bar.set_tokens(62_500, context_tokens=21_000)
    bar.set_cost(0.03)
    await pilot.pause()
    return bar


@pytest.mark.asyncio
async def test_prod_profile_shows_pill_and_red_profile():
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="client-prod", region="us-east-1")
        await pilot.pause()
        pill = bar.query_one("#prod-pill", Static)
        assert pill.display
        assert bar.production_source == "aws"
        cloud = bar.query_one("#cloud-display", Static).render()
        assert _styled(cloud)["client-prod"] == "bold $error"


@pytest.mark.asyncio
async def test_non_prod_profile_hides_pill():
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="client-prod")
        await pilot.pause()
        bar.set_cloud(profile="client-devops", region="ap-south-1")
        await pilot.pause()
        assert not bar.query_one("#prod-pill", Static).display
        cloud = bar.query_one("#cloud-display", Static).render()
        assert isinstance(cloud, Content)
        from opscloud.config.settings import get_glyphs

        glyphs = get_glyphs()
        assert cloud.plain == f"aws client-devops {glyphs.separator} ap-south-1"
        assert _styled(cloud)["client-devops"] == "bold $secondary"


@pytest.mark.asyncio
async def test_k8s_prod_context_lights_pill(monkeypatch):
    monkeypatch.setattr(context_detection, "current_k8s_context", lambda: "eks-prod")
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="client-devops")
        await pilot.pause()
        assert bar.query_one("#prod-pill", Static).display
        assert bar.production_source == "k8s"
        # Only the AWS source turns the profile red.
        cloud = bar.query_one("#cloud-display", Static).render()
        assert _styled(cloud)["client-devops"] == "bold $secondary"


@pytest.mark.asyncio
async def test_tokens_display_contains_meter_and_separators():
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="client-devops", region="ap-south-1")
        await pilot.pause()
        tokens_widget = bar.query_one("#tokens-display", Static).render()
        assert isinstance(tokens_widget, Content)
        plain = tokens_widget.plain
        from opscloud.config.settings import get_glyphs

        glyphs = get_glyphs()
        meter = glyphs.meter_filled + (glyphs.meter_empty * 4)
        expected = f"ctx {meter} 2% {glyphs.box_vertical} 62.5K tok {glyphs.separator} $0.03"
        assert plain == expected
        assert bar.query_one("#sep-tokens", Static).display
        assert bar.query_one("#sep-model", Static).display


@pytest.mark.asyncio
async def test_separator_hides_with_its_neighbour():
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="", region="")
        await pilot.pause()
        # Nothing before tokens, so no leading rule.
        assert not bar.query_one("#sep-tokens", Static).display
        assert bar.query_one("#sep-model", Static).display


@pytest.mark.asyncio
async def test_narrow_drops_cwd_region_model_and_count():
    app = _ThemedApp()
    async with app.run_test(size=(56, 3)) as pilot:
        bar = await _bar(pilot)
        bar.set_cloud(profile="client-devops", region="ap-south-1")
        await pilot.pause()
        assert not bar.query_one("#cwd-display", Static).display
        assert not bar.query_one("#model-display").display
        assert not bar.query_one("#sep-model", Static).display
        cloud_render = bar.query_one("#cloud-display", Static).render()
        assert isinstance(cloud_render, Content)
        assert "ap-south-1" not in cloud_render.plain
        tokens_render = bar.query_one("#tokens-display", Static).render()
        assert isinstance(tokens_render, Content)
        tokens = tokens_render.plain
        from opscloud.config.settings import get_glyphs

        glyphs = get_glyphs()
        meter = glyphs.meter_filled + (glyphs.meter_empty * 4)
        expected = f"ctx {meter} 2% {glyphs.box_vertical} $0.03"
        assert tokens == expected


@pytest.mark.asyncio
async def test_pending_tokens_keep_placeholder():
    app = _ThemedApp()
    async with app.run_test(size=(120, 3)) as pilot:
        bar = await _bar(pilot)
        bar.show_pending_tokens()
        bar.set_cost(0.05)
        await pilot.pause()
        tokens_render = bar.query_one("#tokens-display", Static).render()
        assert isinstance(tokens_render, Content)
        plain = tokens_render.plain
        assert plain.startswith("ctx ...")
        assert "~62.5K tok..." in plain
        assert "$0.05" in plain
