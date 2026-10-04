"""Shared link-click handling for Textual widgets."""

from __future__ import annotations

import ast
import asyncio
from opscloud.utils.logger import get_logger
import webbrowser
from typing import TYPE_CHECKING

from opscloud.security.unicode_security import check_url_safety, strip_dangerous_unicode

if TYPE_CHECKING:
    from textual.app import App
    from textual.events import Click, MouseMove


def _event_app(event: object, app: App | None = None) -> App | None:
    """Return the app for a click event, including real Textual widgets."""
    if app is not None:
        return app
    widget = getattr(event, "widget", None)
    widget_app = getattr(widget, "app", None)
    if widget_app is not None:
        return widget_app
    event_app = getattr(event, "app", None)
    return event_app if event_app is not None else None


logger = get_logger(__name__)


def _notify(
    app: App | None, message: str, *, severity: str, timeout: int | None = None
) -> None:
    """Post a best-effort Textual toast, tolerating apps without `notify`."""
    notify = getattr(app, "notify", None)
    if not callable(notify):
        return
    kwargs: dict[str, object] = {"severity": severity, "markup": False}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        notify(message, **kwargs)
    except (AttributeError, TypeError):
        logger.debug("Could not send notification", exc_info=True)


def _url_open_toasts_enabled() -> bool:
    """Return whether successful URL-open clicks should show a toast."""
    from opscloud.config.manifest import (
        get_option,
        load_config_toml,
        resolve_scalar,
    )

    option = get_option("display.show_url_open_toast")
    if option is None:
        return True
    value, _ = resolve_scalar(option, toml_data=load_config_toml())
    return bool(value)


def _notify_url_opened(app: App | None, url: str) -> None:
    """Show the URL-opened toast when the user has not opted out."""
    if app is None or not _url_open_toasts_enabled():
        return
    _notify(
        app,
        f"Opening URL in default browser: {strip_dangerous_unicode(url)}",
        severity="information",
        timeout=4,
    )


def _link_action_url(click: object) -> str | None:
    """Extract a URL from Textual's Markdown `link(...)` click action."""
    if not isinstance(click, str):
        return None
    if not click.startswith("link(") or not click.endswith(")"):
        return None
    try:
        url = ast.literal_eval(click[len("link(") : -1].strip())
    except (SyntaxError, ValueError):
        return None
    return url if isinstance(url, str) and url else None


def _style_url(style: object) -> str | None:
    """Return a URL from either Rich link style or Textual click metadata."""
    url = getattr(style, "link", None)
    if isinstance(url, str) and url:
        return url
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return None
    return _link_action_url(meta.get("@click"))


def event_targets_link(event: MouseMove) -> bool:
    """Return whether the style under the mouse points to a clickable link."""
    return _style_url(event.style) is not None


async def open_checked_url_async(
    url: str, *, app: App, notify_on_success: bool = False
) -> bool:
    """Open a URL after applying the shared URL safety check."""
    safety = check_url_safety(url)
    if not safety.safe:
        detail = safety.warnings[0] if safety.warnings else "Suspicious URL"
        logger.warning("Blocked suspicious URL: %s (%s)", url, detail)
        _notify(
            app,
            f"Blocked suspicious URL: {strip_dangerous_unicode(url)}\n{detail}",
            severity="warning",
        )
        return False
    return await open_url_async(url, app=app, notify_on_success=notify_on_success)


async def open_url_async(
    url: str, *, app: App, notify_on_success: bool = False
) -> bool:
    """Open url in a browser and toast on failure."""
    try:
        opened = await asyncio.to_thread(webbrowser.open, url)
    except (webbrowser.Error, OSError) as exc:
        logger.warning("webbrowser.open failed for %s: %s", url, exc, exc_info=True)
        opened = False
    if not opened:
        _notify(
            app,
            f"Could not open a browser. URL: {strip_dangerous_unicode(url)}",
            severity="warning",
            timeout=8,
        )
    elif notify_on_success:
        _notify_url_opened(app, url)
    return opened


def open_style_link(event: Click, *, app: App | None = None) -> None:
    """Open the URL from a Rich link style on click, if present."""
    notify_app = _event_app(event, app)
    url = _style_url(event.style)
    if not url:
        return

    safety = check_url_safety(url)
    if not safety.safe:
        detail = safety.warnings[0] if safety.warnings else "Suspicious URL"
        logger.warning("Blocked suspicious URL: %s (%s)", url, detail)
        _notify(
            notify_app,
            f"Blocked suspicious URL: {strip_dangerous_unicode(url)}\n{detail}",
            severity="warning",
        )
        return

    try:
        opened = webbrowser.open(url)
    except (webbrowser.Error, OSError) as exc:
        logger.warning("webbrowser.open failed for %s: %s", url, exc, exc_info=True)
        opened = False
    if not opened:
        _notify(
            notify_app,
            f"Could not open a browser. URL: {strip_dangerous_unicode(url)}",
            severity="warning",
            timeout=8,
        )
        return
    _notify_url_opened(notify_app, url)
    event.stop()
