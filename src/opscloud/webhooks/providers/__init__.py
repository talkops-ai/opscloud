"""Provider adapter registry and default built-in adapters."""

from __future__ import annotations

from opscloud.webhooks.providers.alertmanager import AlertmanagerWebhookAdapter
from opscloud.webhooks.providers.base import (
    WebhookParseError,
    WebhookProviderAdapter,
    WebhookProviderRegistry,
)
from opscloud.webhooks.providers.cloudevents import CloudEventsWebhookAdapter
from opscloud.webhooks.providers.github import GitHubWebhookAdapter
from opscloud.webhooks.providers.jenkins import JenkinsWebhookAdapter
from opscloud.webhooks.providers.observability_itsm import (
    DatadogWebhookAdapter,
    GenericWebhookAdapter,
    ServiceNowWebhookAdapter,
)
from opscloud.webhooks.providers.pagerduty import PagerDutyWebhookAdapter
from opscloud.webhooks.providers.sns import SNSWebhookAdapter


def create_default_registry() -> WebhookProviderRegistry:
    """Create a WebhookProviderRegistry populated with all built-in DevOps & CNCF adapters."""
    registry = WebhookProviderRegistry()
    for adapter in (
        SNSWebhookAdapter(),
        PagerDutyWebhookAdapter(),
        AlertmanagerWebhookAdapter(),
        CloudEventsWebhookAdapter(),
        JenkinsWebhookAdapter(),
        GitHubWebhookAdapter(),
        DatadogWebhookAdapter(),
        ServiceNowWebhookAdapter(),
        GenericWebhookAdapter(),
    ):
        registry.register(adapter)
    return registry


__all__ = [
    "AlertmanagerWebhookAdapter",
    "CloudEventsWebhookAdapter",
    "DatadogWebhookAdapter",
    "GenericWebhookAdapter",
    "GitHubWebhookAdapter",
    "JenkinsWebhookAdapter",
    "PagerDutyWebhookAdapter",
    "SNSWebhookAdapter",
    "ServiceNowWebhookAdapter",
    "WebhookParseError",
    "WebhookProviderAdapter",
    "WebhookProviderRegistry",
    "create_default_registry",
]
