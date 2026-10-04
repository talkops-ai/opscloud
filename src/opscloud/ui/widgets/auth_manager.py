"""Auth manager screen — multi-provider credential management.

Opened via /login or /auth. Lists providers with status, delegates to
AuthPromptScreen for individual key entry.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from textual.app import ComposeResult

from opscloud.model.config import get_credential_env_var
from opscloud.ui.widgets.auth import AuthPromptScreen, AuthResult, PROVIDER_DISPLAY_NAMES

logger = get_logger(__name__)

# Providers to list in the manager
_KNOWN_PROVIDERS: tuple[str, ...] = (
    "google_genai", "openai", "openrouter", "anthropic", "azure_openai",
    "langsmith", "typesafe", "groq", "deepseek", "mistralai", "fireworks", "together",
    "xai", "cohere", "perplexity", "nvidia", "huggingface",
)


class AuthManagerScreen(ModalScreen[None]):
    """Modal listing providers and their credential status.

    Reachable via /login, /auth, /connect. Dismisses with None.
    State changes persisted by AuthPromptScreen.
    """

    class CredentialSaved(Message):
        """Posted when a key is successfully persisted."""
        def __init__(self, provider: str) -> None:
            super().__init__()
            self.provider = provider

    class CredentialDeleted(Message):
        """Posted when a credential is removed."""
        def __init__(self, provider: str) -> None:
            super().__init__()
            self.provider = provider

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("delete", "delete_selected", "Delete key", show=False, priority=True),
    ]

    CSS = """
    AuthManagerScreen {
        align: center middle;
    }

    AuthManagerScreen > Vertical {
        width: 76;
        max-width: 90%;
        height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    AuthManagerScreen .auth-mgr-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    AuthManagerScreen .auth-mgr-desc {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }

    AuthManagerScreen OptionList {
        height: 1fr;
        min-height: 3;
        background: $background;
    }

    AuthManagerScreen .auth-mgr-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 1;
    }
    """

    def __init__(self, *, initial_provider: str | None = None) -> None:
        super().__init__()
        self._initial_provider = initial_provider

    def compose(self) -> ComposeResult:
        options = self._build_options()
        with Vertical():
            yield Static("Manage API Keys", classes="auth-mgr-title")
            yield Static(
                "Select a provider to add, replace, or delete its API key. "
                "Keys are stored in ~/.opscloud/.env.",
                classes="auth-mgr-desc",
            )
            yield OptionList(*options, id="auth-mgr-options")
            yield Static(
                "↑/↓ navigate • Enter add/replace • Delete remove • Esc close",
                classes="auth-mgr-help",
            )

    def on_mount(self) -> None:
        """Highlight initial provider if specified (e.g. from /login anthropic)."""
        self._highlight_initial_provider()

    def _highlight_initial_provider(self) -> None:
        if self._initial_provider is None:
            return
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            index = option_list.get_option_index(self._initial_provider)
            option_list.highlighted = index
            option_list.scroll_to_highlight()
        except Exception:
            pass

    def _build_options(self) -> list[Option]:
        """Build OptionList entries with credential status badges."""
        options = []
        for provider in _KNOWN_PROVIDERS:
            display_name = PROVIDER_DISPLAY_NAMES.get(provider, provider)
            if provider == "langsmith":
                env_var = "LANGSMITH_API_KEY"
                has_key = bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))
            else:
                env_var = get_credential_env_var(provider) or f"{provider.upper()}_API_KEY"
                has_key = bool(os.environ.get(env_var))
            badge = "[stored] ✓" if has_key else "[missing]"
            label = f"{display_name}  {badge}"
            options.append(Option(label, id=provider))
        return options

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        provider = event.option.id
        if not provider:
            return
        env_var = get_credential_env_var(provider) or f"{provider.upper()}_API_KEY"

        def _on_prompt_closed(result: AuthResult | None) -> None:
            if result == AuthResult.SAVED:
                self.post_message(self.CredentialSaved(provider))
            self._refresh_options()

        self.app.push_screen(AuthPromptScreen(provider, env_var), _on_prompt_closed)

    def _refresh_options(self) -> None:
        """Rebuild option list after credential change."""
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            option_list.clear_options()
            for opt in self._build_options():
                option_list.add_option(opt)
        except Exception:
            pass

    def action_delete_selected(self) -> None:
        """Delete the credential for the highlighted provider."""
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            highlighted = option_list.highlighted
            if highlighted is None:
                return
            option = option_list.get_option_at_index(highlighted)
            provider = option.id
            if not provider:
                return

            from opscloud.model.config import revoke_provider_credentials
            settings = getattr(self.app, "_settings", None)
            revoked = revoke_provider_credentials(provider, settings=settings)

            if revoked:
                self.app.notify(
                    f"Removed credentials for {PROVIDER_DISPLAY_NAMES.get(provider, provider)}.",
                    severity="warning",
                    markup=False,
                )
                self.post_message(self.CredentialDeleted(provider))
                self._refresh_options()
            else:
                self.app.notify(
                    f"No stored credentials for {PROVIDER_DISPLAY_NAMES.get(provider, provider)}.",
                    severity="information",
                    markup=False,
                )
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["AuthManagerScreen", "_KNOWN_PROVIDERS"]
