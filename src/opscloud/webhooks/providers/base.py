"""Base protocol and extensible registry for webhook provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import json
from typing import Any

from opscloud.webhooks.models import NormalizedWebhookEvent, WebhookConfig


class WebhookParseError(ValueError):
    """Raised when an incoming webhook payload cannot be decoded or is malformed."""


class WebhookProviderAdapter(ABC):
    """Abstract base class for modular webhook provider adapters."""

    name: str = "generic"
    aliases: tuple[str, ...] = ()
    description: str = "Generic JSON webhook adapter"

    @abstractmethod
    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        """Parse a verified request into a NormalizedWebhookEvent."""

    @staticmethod
    def decode_json_body(raw_body: bytes) -> dict[str, Any]:
        """Decode raw bytes as a JSON object or raise WebhookParseError."""
        if not raw_body or not raw_body.strip():
            raise WebhookParseError("Webhook request body is empty")
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookParseError(f"Invalid JSON payload: {exc}") from exc
        if not isinstance(data, dict):
            raise WebhookParseError("Expected top-level JSON object in webhook payload")
        return data


class WebhookProviderRegistry:
    """Extensible registry mapping provider names and aliases to adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, WebhookProviderAdapter] = {}
        self._aliases: dict[str, str] = {}

    def register(self, adapter: WebhookProviderAdapter) -> None:
        """Register a provider adapter and all of its aliases."""
        primary = adapter.name.lower().strip()
        self._adapters[primary] = adapter
        for alias in adapter.aliases:
            self._aliases[alias.lower().strip()] = primary

    def get(self, provider_or_alias: str) -> WebhookProviderAdapter | None:
        """Look up an adapter by canonical name or alias."""
        key = provider_or_alias.lower().strip()
        canonical = self._aliases.get(key, key)
        return self._adapters.get(canonical)

    def resolve_canonical_name(self, provider_or_alias: str) -> str:
        """Return the canonical provider name for a given route key."""
        key = provider_or_alias.lower().strip()
        return self._aliases.get(key, key)

    def list_providers(self) -> list[dict[str, Any]]:
        """Return metadata for all registered webhook providers."""
        return [
            {
                "provider": name,
                "aliases": list(adapter.aliases),
                "description": adapter.description,
                "endpoint": f"/webhooks/{name}",
            }
            for name, adapter in sorted(self._adapters.items())
        ]
