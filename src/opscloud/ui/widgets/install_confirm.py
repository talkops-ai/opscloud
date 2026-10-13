"""Confirmation modals for package installation in OpsCloud TUI."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Static

from opscloud.model.config import PROVIDER_DISPLAY_NAMES
from opscloud.ui.widgets.modal_base import OpsModalScreen

logger = get_logger(__name__)


class InstallProviderConfirmScreen(OpsModalScreen[bool]):
    """Confirmation modal for installing a model provider's extra.

    Shown when the user selects a model whose provider integration package is not installed.
    Dismisses with True to install and False to cancel.
    """

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Install", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    InstallProviderConfirmScreen .install-confirm-title {
        display: none;
    }

    InstallProviderConfirmScreen .install-confirm-body {
        height: auto;
        color: $text;
        margin-bottom: 1;
    }

    InstallProviderConfirmScreen .install-confirm-help {
        display: none;
    }
    """

    def __init__(
        self, provider: str, extra: str, model_spec: str | None = None
    ) -> None:
        super().__init__()
        self._provider = provider
        self._extra = extra
        self._model_spec = model_spec

    def compose_modal(self):
        provider_name = PROVIDER_DISPLAY_NAMES.get(
            self._provider, self._provider.replace("_", " ").title()
        )
        if self._model_spec is not None:
            body = (
                f"To use [bold]{self._model_spec}[/bold], opscloud needs to "
                f"install the [bold]{self._extra}[/bold] integration. This will add "
                f"the provider package to your opscloud environment."
            )
        else:
            body = (
                f"To add a key for [bold]{provider_name}[/bold], opscloud needs to "
                f"install the [bold]{self._extra}[/bold] integration. This will add "
                f"the provider package to your opscloud environment."
            )

        yield Static(
            f"Install {provider_name} support?",
            classes="install-confirm-title",
        )
        yield Static(
            body,
            classes="install-confirm-body",
            markup=True,
        )
        yield Static(
            "Enter to install, Esc to cancel",
            classes="install-confirm-help",
        )

    def on_mount(self) -> None:
        super().on_mount()
        provider_name = PROVIDER_DISPLAY_NAMES.get(
            self._provider, self._provider.replace("_", " ").title()
        )
        self.set_modal_title(f"Install {provider_name} support?")
        self.set_modal_subtitle([
            ("enter", "install"),
            ("esc", "cancel"),
        ])

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = ["InstallProviderConfirmScreen"]
