"""Authentication prompt modal for capturing and setting provider API keys.

``AuthPromptScreen`` accepts an API key for a single provider or service
(including LangSmith tracing), persists it via environment and local credential
store, and renders provider-specific acquisition guidance with clickable links.

Security notes:

- Inputs are rendered with ``password=True`` so the key is never echoed to
  the terminal.
- This module never logs the key value, never includes it in ``notify()``
  payloads, and never round-trips it through Rich markup.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from opscloud.config.paths import GLOBAL_ENV_PATH, ensure_data_dir, upsert_env_vars
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.color import Color as TColor
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.style import Style as TStyle
from textual.widgets import Checkbox, Input, RadioButton, RadioSet, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click, MouseMove

from opscloud.model.config import get_credential_env_var, get_provider_display_name

logger = get_logger(__name__)


class AuthCheckbox(Checkbox):
    """Checkbox that prevents Enter from toggling it so Enter saves the modal form."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            save_action = getattr(self.screen, "action_save", None)
            if callable(save_action):
                save_action()


# ── Provider API key page URLs ────────────────────────────────────────

PROVIDER_API_KEY_URLS: dict[str, str] = {
    "anthropic": "https://platform.claude.com/login?returnTo=%2Fsettings%2Fkeys",
    "baseten": "https://docs.baseten.co/organization/api-keys",
    "cohere": "https://dashboard.cohere.com/welcome/login?redirect_uri=%2Fapi-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "fireworks": "https://app.fireworks.ai/settings/users/api-keys",
    "google_genai": "https://aistudio.google.com/api-keys",
    "groq": "https://console.groq.com/keys",
    "huggingface": "https://huggingface.co/login?next=%2Fsettings%2Ftokens",
    "ibm": "https://cloud.ibm.com/iam/apikeys",
    "langsmith": "https://smith.langchain.com/settings",
    "litellm": "https://docs.litellm.ai/docs/proxy/virtual_keys",
    "meta": "https://dev.meta.ai/api-keys/",
    "mistralai": "https://console.mistral.ai/api-keys",
    "nvidia": "https://build.nvidia.com/settings/api-keys",
    "openai": "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/workspaces/default/keys",
    "perplexity": "https://www.perplexity.ai/settings/api",
    "together": "https://api.together.ai/settings/api-keys",
    "typesafe": "https://typesafe.ai/keys",
    "xai": "https://console.x.ai/team/default/api-keys",
}

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic",
    "azure_openai": "Azure OpenAI",
    "baseten": "Baseten",
    "cohere": "Cohere",
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks",
    "google_genai": "Google Gemini",
    "google_vertexai": "Google Vertex AI",
    "groq": "Groq",
    "huggingface": "Hugging Face",
    "ibm": "IBM watsonx",
    "langsmith": "LangSmith (tracing)",
    "litellm": "LiteLLM",
    "meta": "Meta",
    "mistralai": "Mistral AI",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "openai_codex": "OpenAI Codex (ChatGPT login)",
    "openrouter": "OpenRouter",
    "perplexity": "Perplexity",
    "together": "Together AI",
    "typesafe": "TypeSafe AI (Jev)",
    "xai": "xAI",
}

CONFIGURATION_DOCS_URL = (
    "https://github.com/talkops-ai/opscloud"
)


def is_langsmith(provider: str) -> bool:
    """Whether provider represents the LangSmith tracing service."""
    return provider.lower() in ("langsmith", "langsmith_tracing", "tracing")


class AuthResult(StrEnum):
    """Outcome of an ``AuthPromptScreen`` interaction."""

    SAVED = "saved"
    """A key was persisted. The caller should retry the original operation."""

    CANCELLED = "cancelled"
    """User dismissed the prompt without saving."""


class AuthPromptScreen(ModalScreen[AuthResult]):
    """Modal that captures and persists an API key for one provider or service.

    Shows provider-specific acquisition instructions with a clickable link
    to the provider's API key page, a storage note, and an F2-toggled
    Advanced section with env-var details, Base URL override, or LangSmith
    region/project controls.
    """

    AUTO_FOCUS = "#auth-prompt-input"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("f2", "toggle_advanced", "Advanced", show=False, priority=True),
        Binding("enter", "save", "Save", show=False, priority=True),
    ]

    CSS = """
    AuthPromptScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    AuthPromptScreen > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 90vh;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    AuthPromptScreen .auth-prompt-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    AuthPromptScreen .auth-prompt-copy,
    AuthPromptScreen .auth-prompt-instructions,
    AuthPromptScreen .auth-prompt-meta {
        height: auto;
        color: $text;
        margin-bottom: 1;
    }

    AuthPromptScreen #auth-prompt-region {
        height: auto;
        width: 100%;
        margin-bottom: 1;
        border: solid $panel;
    }

    AuthPromptScreen #auth-prompt-input,
    AuthPromptScreen #auth-prompt-base-url,
    AuthPromptScreen #auth-prompt-project-input {
        margin-bottom: 1;
        border: solid $panel;
    }

    AuthPromptScreen #auth-prompt-input:focus,
    AuthPromptScreen #auth-prompt-base-url:focus,
    AuthPromptScreen #auth-prompt-project-input:focus {
        border: solid $primary;
    }

    AuthPromptScreen .auth-prompt-error {
        height: auto;
        color: $error;
        margin-bottom: 1;
    }

    AuthPromptScreen .auth-prompt-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        text-align: center;
    }
    """

    def __init__(
        self,
        provider: str,
        env_var: str | None = None,
        *,
        reason: str | None = None,
        allow_empty_submit: bool = False,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._env_var = (
            env_var
            or get_credential_env_var(provider)
            or ("LANGSMITH_API_KEY" if is_langsmith(provider) else f"{provider.upper()}_API_KEY")
        )
        self._reason = reason
        self._allow_empty_submit = allow_empty_submit
        self._advanced_visible = False
        self._is_langsmith = is_langsmith(provider)

    def compose(self) -> ComposeResult:
        provider_label = self._get_display_name()
        with Vertical():
            # ── Title ──
            yield Static(
                Content.from_markup(
                    "API key for [bold]$provider[/bold]",
                    provider=provider_label,
                ),
                classes="auth-prompt-title",
            )

            # ── Reason ──
            if self._reason:
                yield Static(
                    Content.from_markup("$reason", reason=self._reason),
                    classes="auth-prompt-copy",
                )

            # ── Instructions with clickable link ──
            yield Static(
                self._build_key_instructions(),
                classes="auth-prompt-instructions",
                id="auth-prompt-key-instructions",
            )

            # ── Key input ──
            initial_val = ""
            if self._is_langsmith:
                initial_val = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY") or ""
            yield Input(
                value=initial_val,
                placeholder="Paste your API key",
                password=True,
                id="auth-prompt-input",
            )

            # ── Storage note ──
            if self._is_langsmith:
                yield Static(
                    Content.from_markup(
                        "opscloud stores the above key locally and turns on LangSmith tracing. "
                        "To pause tracing without removing the key, set [bold]LANGSMITH_TRACING=false[/bold]."
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-storage-note",
                )
            else:
                yield Static(
                    Content.from_markup(
                        "opscloud stores the above key locally and uses it "
                        "when you select [bold]$provider[/bold] models.",
                        provider=provider_label,
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-storage-note",
                )

            # ── Advanced toggle ──
            yield Static(
                self._build_advanced_toggle_label(),
                classes="auth-prompt-advanced-toggle",
                id="auth-prompt-advanced-toggle",
            )

            # ── Advanced: env-var details (hidden by default) ──
            if self._env_var:
                key_meta = Static(
                    Content.assemble(
                        "Alternatively, environment variables can be used in place "
                        "of the key stored above. Set ",
                        (self._env_var, TStyle(bold=True)),
                        " to share a key with other provider SDK tools. "
                        "After setting one in a .env file, restart opscloud to pick "
                        "it up. ",
                        (
                            "Configuration docs",
                            self._link_style(CONFIGURATION_DOCS_URL),
                        ),
                        ".",
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-key-meta",
                )
                key_meta.display = self._advanced_visible
                yield key_meta

            if self._is_langsmith:
                lbl_region = Static(
                    Content.from_markup("[bold]LangSmith region[/bold]"),
                    classes="auth-prompt-meta",
                    id="auth-prompt-region-label",
                )
                lbl_region.display = self._advanced_visible
                yield lbl_region

                current_endpoint = os.environ.get("LANGSMITH_ENDPOINT") or ""
                is_eu = "eu.api.smith" in current_endpoint
                is_custom = bool(current_endpoint and not is_eu and "api.smith" not in current_endpoint)

                radio_set = RadioSet(
                    RadioButton("United States (default)", value=not (is_eu or is_custom), id="reg-us"),
                    RadioButton("Europe", value=is_eu, id="reg-eu"),
                    RadioButton("Custom (self-hosted / proxy)", value=is_custom, id="reg-custom"),
                    id="auth-prompt-region",
                )
                radio_set.display = self._advanced_visible
                yield radio_set

                base_url_input = Input(
                    value=current_endpoint if is_custom else "",
                    placeholder="https://my-langsmith.example.com",
                    id="auth-prompt-base-url",
                )
                base_url_input.display = self._advanced_visible and is_custom
                yield base_url_input

                base_url_hint = Static(
                    Content.from_markup(
                        "Point tracing at a self-hosted or proxied LangSmith. "
                        "Sets [bold]LANGSMITH_ENDPOINT[/bold]; must be an http(s) URL."
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-base-url-hint",
                )
                base_url_hint.display = self._advanced_visible and is_custom
                yield base_url_hint

                lbl_proj = Static(
                    Content.from_markup("[bold]Project name[/bold]"),
                    classes="auth-prompt-meta",
                    id="auth-prompt-project-label",
                )
                lbl_proj.display = self._advanced_visible
                yield lbl_proj

                current_proj = os.environ.get("LANGSMITH_PROJECT") or os.environ.get("OPSCODE_LANGSMITH_PROJECT") or "opscloud"
                proj_input = Input(
                    value=current_proj,
                    placeholder="LANGSMITH_PROJECT (default: opscloud)",
                    id="auth-prompt-project-input",
                )
                proj_input.display = self._advanced_visible
                yield proj_input

                proj_hint = Static(
                    Content.from_markup(
                        "Route agent traces to this LangSmith project. "
                        "Leave blank to use the default [bold]opscloud[/bold]."
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-project-hint",
                )
                proj_hint.display = self._advanced_visible
                yield proj_hint
            else:
                base_url_label = Static(
                    Content.from_markup("[bold]Base URL override[/bold]"),
                    classes="auth-prompt-meta",
                    id="auth-prompt-base-url-label",
                )
                base_url_label.display = self._advanced_visible
                yield base_url_label

                base_url_input = Input(
                    value="",
                    placeholder="Base URL",
                    id="auth-prompt-base-url",
                )
                base_url_input.display = self._advanced_visible
                yield base_url_input

                base_url_hint = Static(
                    Content.from_markup(
                        "Override the provider endpoint for this stored key. "
                        "Leave blank to use the provider default."
                    ),
                    classes="auth-prompt-meta",
                    id="auth-prompt-base-url-hint",
                )
                base_url_hint.display = self._advanced_visible
                yield base_url_hint

                if self._provider == "google_genai":
                    from opscloud.config.settings import resolve_env_var

                    v_env = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or resolve_env_var("GOOGLE_GENAI_USE_VERTEXAI") or ""
                    is_vertex = v_env.lower() in ("true", "1")

                    vertex_cb = AuthCheckbox(
                        "Use Vertex AI backend (GOOGLE_GENAI_USE_VERTEXAI)",
                        value=is_vertex,
                        id="auth-prompt-vertexai-cb",
                    )
                    vertex_cb.display = self._advanced_visible
                    yield vertex_cb

            yield Static("", classes="auth-prompt-error", id="auth-prompt-error")

            yield Static(
                "Enter save • Esc cancel • F2 advanced",
                classes="auth-prompt-help",
            )

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if not self._is_langsmith:
            return
        pressed = getattr(event, "pressed", None)
        is_custom = getattr(pressed, "id", None) == "reg-custom"
        for sel in ("#auth-prompt-base-url", "#auth-prompt-base-url-hint"):
            for w in self.query(sel):
                w.display = is_custom

    def _get_display_name(self) -> str:
        return PROVIDER_DISPLAY_NAMES.get(
            self._provider,
            get_provider_display_name(self._provider),
        )

    def _link_style(self, url: str) -> TStyle:
        return TStyle(bold=True, underline=True, link=url)

    def _build_key_instructions(self) -> Content:
        provider_label = self._get_display_name()
        url = PROVIDER_API_KEY_URLS.get(self._provider)

        if self._is_langsmith:
            text = f"Sign in to {provider_label}, create or copy an API key, then paste it below. "
        elif self._provider == "azure_openai":
            text = (
                "Find your key in your Azure OpenAI resource's "
                "Keys and Endpoint page, then paste it below. "
            )
        elif self._provider == "openai":
            text = (
                f"Sign in to {provider_label}, create or copy an API key, then "
                "paste it below. Minimum permissions needed: "
                "under Model capabilities, grant Write access to Responses "
                "(/v1/responses). For older models, you may also need "
                "Request access to Chat completions (/v1/chat/completions). "
            )
        else:
            text = f"Sign in to {provider_label}, create or copy an API key, then paste it below. "

        if url:
            label = f"{provider_label} key page"
            return Content.assemble(text, (label, self._link_style(url)))
        return Content(text)

    def _build_advanced_toggle_label(self) -> str:
        marker = "▾" if self._advanced_visible else "▸"
        return f"{marker} Advanced (F2)"

    def action_toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        if self._is_langsmith:
            is_custom = False
            try:
                is_custom = self.query_one("#reg-custom", RadioButton).value
            except Exception:
                pass
            for selector in (
                "#auth-prompt-key-meta",
                "#auth-prompt-region-label",
                "#auth-prompt-region",
                "#auth-prompt-project-label",
                "#auth-prompt-project-input",
                "#auth-prompt-project-hint",
            ):
                for widget in self.query(selector):
                    widget.display = self._advanced_visible
            for selector in ("#auth-prompt-base-url", "#auth-prompt-base-url-hint"):
                for widget in self.query(selector):
                    widget.display = self._advanced_visible and is_custom
        else:
            for selector in (
                "#auth-prompt-key-meta",
                "#auth-prompt-base-url-label",
                "#auth-prompt-base-url",
                "#auth-prompt-base-url-hint",
                "#auth-prompt-vertexai-cb",
            ):
                for widget in self.query(selector):
                    widget.display = self._advanced_visible

        self.query_one("#auth-prompt-advanced-toggle", Static).update(
            self._build_advanced_toggle_label()
        )
        if not self._advanced_visible:
            self.query_one("#auth-prompt-input", Input).focus()

    def on_click(self, event: Click) -> None:
        widget = event.widget
        if (
            widget is not None
            and widget.id == "auth-prompt-advanced-toggle"
            and not event.style.link
        ):
            self.action_toggle_advanced()
            event.stop()
            return
        if event.style.link:
            import webbrowser
            webbrowser.open(event.style.link)
            event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        widget = event.widget
        self.styles.pointer = (
            "pointer"
            if event.style.link
            or (widget is not None and widget.id == "auth-prompt-advanced-toggle")
            else "default"
        )

    def on_leave(self) -> None:
        self.styles.pointer = "default"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "toggle_advanced", "Advanced"),
        Binding("enter", "save", "Save"),
    ]

    def action_save(self) -> None:
        self._perform_save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._perform_save()

    def _perform_save(self) -> None:
        provider_label = self._get_display_name()
        cleaned = self.query_one("#auth-prompt-input", Input).value.strip()

        if self._is_langsmith:
            if not cleaned:
                if self._allow_empty_submit:
                    self.dismiss(AuthResult.CANCELLED)
                    return
                else:
                    self._show_error("API key cannot be empty.")
                    return

            proj = "opscloud"
            try:
                proj = self.query_one("#auth-prompt-project-input", Input).value.strip() or "opscloud"
            except Exception:
                pass

            endpoint = "https://api.smith.langchain.com"
            try:
                if self.query_one("#reg-eu", RadioButton).value:
                    endpoint = "https://eu.api.smith.langchain.com"
                elif self.query_one("#reg-custom", RadioButton).value:
                    endpoint = self.query_one("#auth-prompt-base-url", Input).value.strip()
            except Exception:
                pass

            os.environ["LANGSMITH_API_KEY"] = cleaned
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGSMITH_PROJECT"] = proj
            if endpoint:
                os.environ["LANGSMITH_ENDPOINT"] = endpoint

            try:
                ls_vars = {
                    "LANGSMITH_API_KEY": cleaned,
                    "LANGSMITH_TRACING": "true",
                    "LANGSMITH_PROJECT": proj,
                }
                if endpoint:
                    ls_vars["LANGSMITH_ENDPOINT"] = endpoint
                upsert_env_vars(ls_vars)
            except Exception as e:
                logger.debug("Failed to persist LangSmith credentials to file: %s", e)

            try:
                self.app.notify(
                    f"Successfully saved authentication for {provider_label}.",
                    severity="information",
                    markup=False,
                )
            except Exception:
                pass
            self.dismiss(AuthResult.SAVED)
            return

        base_url = ""
        try:
            base_url = self.query_one("#auth-prompt-base-url", Input).value.strip()
        except Exception:
            pass

        use_vertex = False
        vertex_env_val = None
        if self._provider == "google_genai":
            try:
                from textual.widgets import Checkbox
                vertex_cb = self.query_one("#auth-prompt-vertexai-cb", Checkbox)
                use_vertex = vertex_cb.value
            except Exception:
                use_vertex = False

            vertex_env_val = "true" if use_vertex else "false"
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = vertex_env_val

        if not cleaned:
            if self._provider == "google_genai" and use_vertex:
                pass
            elif self._allow_empty_submit:
                self.dismiss(AuthResult.CANCELLED)
                return
            else:
                self._show_error("API key cannot be empty (unless Vertex AI backend is enabled).")
                return

        if cleaned:
            os.environ[self._env_var] = cleaned
            logger.info("Saved environment variable credentials for %s (%s)", self._provider, self._env_var)

        if base_url:
            base_url_var = f"{self._provider.upper()}_BASE_URL"
            os.environ[base_url_var] = base_url
            logger.info("Set base URL for %s: %s", self._provider, base_url_var)

        try:
            prov_vars = {}
            if cleaned:
                prov_vars[self._env_var] = cleaned
            if base_url:
                base_url_var = f"{self._provider.upper()}_BASE_URL"
                prov_vars[base_url_var] = base_url
            if vertex_env_val is not None:
                prov_vars["GOOGLE_GENAI_USE_VERTEXAI"] = vertex_env_val
            if prov_vars:
                upsert_env_vars(prov_vars)
        except Exception as e:
            logger.debug("Failed to persist credentials to file: %s", e)

        try:
            self.app.notify(
                f"Successfully saved authentication for {provider_label}.",
                severity="information",
                markup=False,
            )
        except Exception:
            pass
        self.dismiss(AuthResult.SAVED)

    def _show_error(self, msg: str) -> None:
        try:
            err = self.query_one("#auth-prompt-error", Static)
            err.update(msg)
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(AuthResult.CANCELLED)


__all__ = [
    "AuthCheckbox",
    "AuthPromptScreen",
    "AuthResult",
    "CONFIGURATION_DOCS_URL",
    "PROVIDER_API_KEY_URLS",
    "PROVIDER_DISPLAY_NAMES",
    "is_langsmith",
]
