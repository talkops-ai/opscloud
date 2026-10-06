"""Unit tests for OpsCloud 22-provider model catalog, spec resolution, and reasoning effort."""

import pytest
from opscloud.model.config import (
    NO_AUTH_REQUIRED_PROVIDERS,
    PROVIDER_API_KEY_ENV,
    detect_provider,
    format_token_count,
    get_provider_display_name,
    resolve_model_spec,
)
from opscloud.model.factory import create_model
from opscloud.model.reasoning import (
    default_effort_for_model,
    is_effort_supported_for_model,
    supported_efforts_for_model,
)

ALL_22_PROVIDERS = [
    "anthropic",
    "openai",
    "azure_openai",
    "google_genai",
    "google_vertexai",
    "bedrock",
    "groq",
    "deepseek",
    "openrouter",
    "ollama",
    "together",
    "fireworks",
    "mistralai",
    "cohere",
    "perplexity",
    "baseten",
    "huggingface",
    "nvidia",
    "xai",
    "ibm",
    "litellm",
    "meta",
]


def test_22_provider_catalog_completeness():
    for provider in ALL_22_PROVIDERS:
        assert (
            provider in PROVIDER_API_KEY_ENV or provider in NO_AUTH_REQUIRED_PROVIDERS
        ), f"Missing API key or no-auth mapping for {provider}"
        display_name = get_provider_display_name(provider)
        assert display_name is not None and len(display_name) > 0, f"Missing display name for {provider}"


def test_resolve_model_spec():
    assert resolve_model_spec("anthropic:claude-3-5-sonnet") == ("anthropic", "claude-3-5-sonnet")
    assert resolve_model_spec("openai:gpt-4o") == ("openai", "gpt-4o")
    assert resolve_model_spec("bedrock:anthropic.claude-3-7-sonnet-20250219-v1:0") == (
        "bedrock",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
    )
    assert resolve_model_spec("deepseek:deepseek-chat") == ("deepseek", "deepseek-chat")
    assert resolve_model_spec("ollama:llama3.3") == ("ollama", "llama3.3")

    # Bare model name detection
    prov, m_id = resolve_model_spec("gpt-4o")
    assert prov == "openai"
    assert m_id == "gpt-4o"

    prov_c, m_c = resolve_model_spec("claude-3-5-sonnet")
    assert prov_c == "anthropic"

    # Empty or None spec does not default to any provider
    assert resolve_model_spec(None) == ("", "")
    assert resolve_model_spec("") == ("", "")


def test_detect_provider():
    assert detect_provider("gpt-4o") == "openai"
    assert detect_provider("claude-3-7-sonnet") == "anthropic"
    assert detect_provider("gemini-3.8-flash") == "google_genai"
    assert detect_provider("gemini-2.5-flash") == "google_genai"
    assert detect_provider("deepseek-chat") == "deepseek"
    assert detect_provider("mistral-large") == "mistralai"
    assert detect_provider("groq-something") == "groq"
    assert detect_provider("llama-3.3-70b-versatile") == "ollama"


def test_reasoning_effort_support():
    # OpenAI reasoning models
    assert is_effort_supported_for_model("openai:o3-mini", "high") is True
    assert is_effort_supported_for_model("openai:o3-mini", "medium") is True
    assert is_effort_supported_for_model("openai:gpt-4o", "high") is False

    efforts = supported_efforts_for_model("openai:o3-mini")
    assert "low" in efforts and "medium" in efforts and "high" in efforts

    # Default effort check
    assert default_effort_for_model("openai:o3-mini") in ("medium", "high", "low")


def test_format_token_counts():
    assert format_token_count(1_000_000) == "1M"
    assert format_token_count(128_000) == "128k"
    assert format_token_count(8_192) == "8.2k"
    assert format_token_count(500) == "500"


def test_create_model_missing_credentials(monkeypatch):
    # Ensure credential env var is unset
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPSCLOUD_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(Exception) as exc_info:
        create_model("anthropic:claude-3-5-sonnet")
    assert "credentials" in str(exc_info.value).lower() or "api_key" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_create_model_ollama_local(monkeypatch):
    from opscloud.exceptions import MissingProviderPackageError
    from opscloud.model.factory import clear_model_cache
    from unittest.mock import MagicMock

    clear_model_cache()
    # When package is missing, it raises MissingProviderPackageError; when present, it instantiates
    try:
        model = create_model("ollama:llama3.3")
        assert model is not None
    except MissingProviderPackageError as e:
        assert e.provider == "ollama"
        assert "langchain-ollama" in e.package

    clear_model_cache()
    # When mocked, it returns the model instance
    mock_model = MagicMock()
    monkeypatch.setattr("langchain.chat_models.init_chat_model", lambda *args, **kwargs: mock_model)
    res = create_model("ollama:llama3.3")
    assert res.model is mock_model
    assert res.provider == "ollama"
    clear_model_cache()


def test_bedrock_model_detection_and_resolution():
    from opscloud.model.config import is_bedrock_model_id, normalize_model_spec

    # Bare Bedrock IDs with version colons
    sonnet_v2 = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    us_sonnet = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
    llama_70b = "meta.llama3-3-70b-instruct-v1:0"
    nova_pro = "amazon.nova-pro-v1:0"

    assert is_bedrock_model_id(sonnet_v2) is True
    assert is_bedrock_model_id(us_sonnet) is True
    assert is_bedrock_model_id(llama_70b) is True
    assert is_bedrock_model_id(nova_pro) is True
    assert is_bedrock_model_id("gpt-4o") is False
    assert is_bedrock_model_id("claude-3-5-sonnet") is False

    # detect_provider
    assert detect_provider(sonnet_v2) == "bedrock"
    assert detect_provider(us_sonnet) == "bedrock"
    assert detect_provider(llama_70b) == "bedrock"
    assert detect_provider(nova_pro) == "bedrock"

    # resolve_model_spec does not break on version colon (:0)
    assert resolve_model_spec(sonnet_v2) == ("bedrock", sonnet_v2)
    assert resolve_model_spec(us_sonnet) == ("bedrock", us_sonnet)
    assert resolve_model_spec(f"bedrock:{sonnet_v2}") == ("bedrock", sonnet_v2)

    # normalize_model_spec
    assert normalize_model_spec(sonnet_v2) == f"bedrock:{sonnet_v2}"
    assert normalize_model_spec(f"bedrock:{sonnet_v2}") == f"bedrock:{sonnet_v2}"


def test_bedrock_auth_status_and_kwargs(monkeypatch):
    from opscloud.model.config import ProviderAuthState, get_provider_auth_status
    from opscloud.model.factory import _get_provider_kwargs

    # Clean env
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    # Incomplete credentials (access key without secret)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
    status = get_provider_auth_status("bedrock")
    assert status.state == ProviderAuthState.MISSING

    # Complete credentials
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_TEST")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    status = get_provider_auth_status("bedrock")
    assert status.state == ProviderAuthState.CONFIGURED

    # Verify provider kwargs injection
    kwargs = _get_provider_kwargs("bedrock", model_name="anthropic.claude-3-7-sonnet-20250219-v1:0")
    assert kwargs.get("region_name") == "eu-central-1"
    assert kwargs.get("aws_access_key_id") == "AKIA_TEST"
    assert kwargs.get("aws_secret_access_key") == "SECRET_TEST"
    assert "api_key" not in kwargs


def test_no_default_model_or_provider_without_user_selection(monkeypatch):
    """Ensure that the system never imposes a default model or provider on the user."""
    from opscloud.config.settings import get_settings
    from opscloud.exceptions import NoCredentialsConfiguredError
    from opscloud.model.config import ModelConfig
    from opscloud.model.factory import _get_default_model_spec

    # Clear any credentials that might be in env
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "AWS_ACCESS_KEY_ID",
    ):
        monkeypatch.delenv(k, raising=False)

    # Empty settings without model
    s = get_settings()
    monkeypatch.setattr(s, "model_name", None)
    monkeypatch.setattr(s, "model", None)

    # ModelConfig.load() has no default forced
    monkeypatch.setattr("opscloud.config.toml_config.load_default_model", lambda: None)
    monkeypatch.setattr("opscloud.config.toml_config.load_recent_model", lambda: None)
    cfg = ModelConfig.load()
    assert cfg.default_model is None
    assert cfg.recent_model is None

    # has_provider_credentials returns False for key-based providers when keys are unset
    monkeypatch.setattr("opscloud.model.factory.has_provider_credentials", lambda prov: False)

    # When no model is configured and no credentials exist, _get_default_model_spec raises
    with pytest.raises(NoCredentialsConfiguredError, match="No model is configured yet"):
        _get_default_model_spec()


def test_unified_model_config_consistency():
    from opscloud.model.config import (
        AVAILABLE_MODELS,
        DEFAULT_PROVIDER_PRIORITY,
        RECOMMENDED_SPECS,
        get_available_models_list,
        get_curated_models_for_provider,
    )

    # 1. DEFAULT_PROVIDER_PRIORITY covers all providers in AVAILABLE_MODELS
    for prov in AVAILABLE_MODELS:
        assert prov in DEFAULT_PROVIDER_PRIORITY, f"{prov} missing from DEFAULT_PROVIDER_PRIORITY"

    # 2. RECOMMENDED_SPECS includes "auto" and all curated models in AVAILABLE_MODELS
    assert "auto" in RECOMMENDED_SPECS
    for prov, models in AVAILABLE_MODELS.items():
        for model_id, _ in models:
            spec = f"{prov}:{model_id}"
            assert spec in RECOMMENDED_SPECS, f"{spec} missing from RECOMMENDED_SPECS"

    # 3. get_curated_models_for_provider returns all curated models
    genai_curated = get_curated_models_for_provider("google_genai")
    assert len(genai_curated) >= 12
    model_ids = [m[0] for m in genai_curated]
    assert "gemini-3.8-flash" in model_ids
    assert "gemini-3.7-flash" in model_ids
    assert "gemini-3.6-flash" in model_ids
    assert "gemini-3.5-flash" in model_ids
    assert "gemini-3.5-flash-lite" in model_ids

    # 4. get_available_models_list has curated models at the beginning
    avail = get_available_models_list()
    avail_specs = [m[0] for m in avail]
    assert "google_genai:gemini-3.8-flash" in avail_specs
    assert "google_genai:gemini-3.7-flash" in avail_specs
    assert "google_genai:gemini-3.6-flash" in avail_specs
    assert "google_vertexai:gemini-3.8-flash" in avail_specs
    assert "openrouter:google/gemini-3.8-flash" in avail_specs
    assert "litellm:gemini/gemini-3.8-flash" in avail_specs


def test_model_selector_prioritizes_active_and_shows_all_curated_models():
    from opscloud.ui.widgets.model_selector import ModelSelectorScreen

    screen = ModelSelectorScreen(
        current_model="gemini-3.7-flash",
        current_provider="google_genai",
    )
    screen.on_mount()

    # Recommended view includes all curated models + auto
    assert screen._recommended_only is True
    filtered_specs = [m[0] for m in screen._filtered_models]
    assert "auto" in filtered_specs
    assert "google_genai:gemini-3.8-flash" in filtered_specs
    assert "google_genai:gemini-3.7-flash" in filtered_specs
    assert "google_genai:gemini-3.6-flash" in filtered_specs
    assert "google_genai:gemini-3.5-flash" in filtered_specs
    assert "google_genai:gemini-3.5-flash-lite" in filtered_specs

    # Provider grouping sorts active provider first
    groups = {}
    for spec, name, prov in screen._filtered_models:
        groups.setdefault(prov, []).append((spec, name, prov))

    def _sort_key(prov: str):
        from opscloud.model.config import DEFAULT_PROVIDER_PRIORITY, get_provider_auth_status
        if prov == "dynamic":
            return (0, 0, prov)
        if screen._current_provider and prov == screen._current_provider:
            return (1, 0, prov)
        auth = get_provider_auth_status(prov)
        is_authed = auth.as_legacy_bool() is True
        tier = 2 if is_authed else 3
        p_idx = DEFAULT_PROVIDER_PRIORITY.index(prov) if prov in DEFAULT_PROVIDER_PRIORITY else 99
        return (tier, p_idx, prov)

    sorted_provs = sorted(groups.keys(), key=_sort_key)
    assert sorted_provs[0] == "dynamic"
    assert sorted_provs[1] == "google_genai"


def test_pool_selector_and_model_selector_model_consistency():
    from opscloud.model.config import get_curated_models_for_provider
    from opscloud.ui.widgets.pool_selector import PoolSelectorScreen

    ps = PoolSelectorScreen(active_provider="google_genai")
    pool_options = ps._get_model_options_for_provider("google_genai")
    curated = get_curated_models_for_provider("google_genai")

    assert len(pool_options) == len(curated)
    for (disp, spec), (cur_id, cur_disp) in zip(pool_options, curated):
        assert spec == f"google_genai:{cur_id}"


def test_model_option_hover_and_in_place_selection():
    from opscloud.ui.widgets.model_selector import ModelOption

    opt = ModelOption(
        label="  Gemini 3.7 Flash",
        model_spec="google_genai:gemini-3.7-flash",
        provider="google_genai",
        index=3,
        display_name="Gemini 3.7 Flash",
        is_selected=False,
    )
    assert not opt.is_selected
    assert "model-option-selected" not in opt.classes

    # Toggle selected True in-place
    opt.set_selected(True)
    assert opt.is_selected
    assert "model-option-selected" in opt.classes
    assert "› Gemini 3.7 Flash" in opt.render_label_text()

    # Toggle selected False in-place
    opt.set_selected(False)
    assert not opt.is_selected
    assert "model-option-selected" not in opt.classes
    assert "  Gemini 3.7 Flash" in opt.render_label_text()


def test_model_selector_smooth_navigation_and_jev_system1():
    from opscloud.ui.widgets.model_selector import ModelOption, ModelSelectorScreen

    screen = ModelSelectorScreen(
        current_model="gemini-3.5-flash",
        current_provider="google_genai",
    )
    screen.on_mount()

    # Verify Jev entry is clean, accurate, and has NO emojis
    auto_model = next(m for m in screen._all_models if m[0] == "auto")
    assert "⚡" not in auto_model[1]
    assert "TypeSafe Jev System 1" in auto_model[1]

    # Mock option widgets for screen
    opt0 = ModelOption(
        label="› Option 0",
        model_spec="auto",
        provider="dynamic",
        index=0,
        display_name="Option 0",
        is_selected=True,
    )
    opt1 = ModelOption(
        label="  Option 1",
        model_spec="google_genai:gemini-3.7-flash",
        provider="google_genai",
        index=1,
        display_name="Option 1",
        is_selected=False,
    )
    screen._option_widgets = [opt0, opt1]
    screen._flat_order = [("auto", "Option 0", "dynamic"), ("google_genai:gemini-3.7-flash", "Option 1", "google_genai")]
    screen._selected_index = 0

    # Moving down: updates widgets in-place without rebuilding DOM
    screen._move_selection(1)
    assert screen._selected_index == 1
    assert not opt0.is_selected
    assert opt1.is_selected

    # Hovering over option 0: updates selection back to 0 without rebuilding DOM
    hover_event = ModelOption.Hovered(index=0)
    screen.on_model_option_hovered(hover_event)
    assert screen._selected_index == 0
    assert opt0.is_selected
    assert not opt1.is_selected


async def test_model_selector_dismiss_idempotency_and_auth_check(monkeypatch: pytest.MonkeyPatch):
    """Verify ModelSelectorScreen dismissal is idempotent and prevents ScreenStackError."""
    from opscloud.ui.app import OpsCloudApp
    from opscloud.ui.widgets.model_selector import ModelSelectorScreen

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-mock")
    app = OpsCloudApp()
    async with app.run_test(headless=True) as pilot:
        screen = ModelSelectorScreen()
        results = []
        app.push_screen(screen, lambda res: results.append(res))
        await pilot.pause()
        assert len(app._screen_stack) == 2

        # 1st call via _select_with_auth_check
        screen._select_with_auth_check("google_genai:gemini-3.6-flash", "google_genai", None)
        await pilot.pause()
        assert screen._dismissed is True
        assert len(app._screen_stack) == 1
        assert len(results) == 1

        # 2nd call (simulating double click or rapid re-trigger that previously caused ScreenStackError)
        screen._select_with_auth_check("google_genai:gemini-3.6-flash", "google_genai", None)
        await pilot.pause()
        assert len(app._screen_stack) == 1
        assert len(results) == 1


def test_model_option_multi_click_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ModelOption ignores rapid multi-clicks (chain > 1)."""
    from opscloud.ui.widgets.model_selector import ModelOption
    from textual.events import Click
    from textual.message import Message

    opt = ModelOption(
        label="  Gemini 3.6 Flash",
        model_spec="google_genai:gemini-3.6-flash",
        provider="google_genai",
        index=0,
    )

    posted: list[Message] = []

    def fake_post_message(message: Message) -> bool:
        posted.append(message)
        return True

    monkeypatch.setattr(opt, "post_message", fake_post_message)

    # First click (chain=1) -> posted
    click1 = Click(opt, x=0, y=0, delta_x=0, delta_y=0, button=1, shift=False, meta=False, ctrl=False, chain=1)
    opt.on_click(click1)
    assert len(posted) == 1
    assert isinstance(posted[0], ModelOption.Clicked)
    assert posted[0].model_spec == "google_genai:gemini-3.6-flash"

    # Second click of a double-click (chain=2) -> suppressed!
    click2 = Click(opt, x=0, y=0, delta_x=0, delta_y=0, button=1, shift=False, meta=False, ctrl=False, chain=2)
    opt.on_click(click2)
    assert len(posted) == 1


async def test_app_pop_screen_guards_against_screen_stack_error():
    """Verify OpsCloudApp.pop_screen does not raise ScreenStackError when stack size <= 1."""
    from opscloud.ui.app import OpsCloudApp

    app = OpsCloudApp()
    async with app.run_test(headless=True):
        assert len(app._screen_stack) == 1
        # Should not raise ScreenStackError!
        res = app.pop_screen()
        assert res is not None


def test_gemini_3_8_flash_spec_and_reasoning_profiles():
    """Verify Gemini 3.8 Flash registration, capabilities, and reasoning effort across providers."""
    from opscloud.model.config import get_model_profile
    from opscloud.model.reasoning import (
        default_effort_for_model,
        is_effort_supported_for_model,
        supported_efforts_for_model,
    )

    gemini_3_8_specs = [
        "google_genai:gemini-3.8-flash",
        "google_vertexai:gemini-3.8-flash",
        "openrouter:google/gemini-3.8-flash",
        "litellm:gemini/gemini-3.8-flash",
    ]

    for spec in gemini_3_8_specs:
        entry = get_model_profile(spec)
        assert entry is not None, f"Profile missing for {spec}"
        profile = entry.get("profile", {})
        assert profile.get("max_input_tokens") == 1_000_000, f"Max input tokens mismatch for {spec}"
        assert profile.get("max_output_tokens") == 64_000, f"Max output tokens mismatch for {spec}"
        assert profile.get("tool_calling") is True, f"Tool calling should be enabled for {spec}"
        assert profile.get("structured_output") is True, f"Structured output should be enabled for {spec}"
        assert profile.get("reasoning_output") is True, f"Reasoning output should be enabled for {spec}"
        assert profile.get("reasoning_effort_levels") == ["low", "medium", "high"], f"Reasoning levels mismatch for {spec}"
        assert profile.get("reasoning_effort_default") == "medium", f"Default reasoning mismatch for {spec}"

        # Reasoning effort helpers
        assert is_effort_supported_for_model(spec, "low") is True
        assert is_effort_supported_for_model(spec, "medium") is True
        assert is_effort_supported_for_model(spec, "high") is True
        assert default_effort_for_model(spec) == "medium"
        assert set(supported_efforts_for_model(spec)) == {"low", "medium", "high"}
