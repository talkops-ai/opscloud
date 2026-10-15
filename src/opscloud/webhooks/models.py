"""Data models and configuration for OpsCloud central webhook ingestion and dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Any

from opscloud.integrations.base import IncomingMessage

if TYPE_CHECKING:
    from opscloud.config.settings import Settings


class WebhookSeverity(StrEnum):
    """Normalized severity levels across monitoring, paging, and CI/CD providers."""

    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    INFO = "info"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: str | int | None) -> WebhookSeverity:
        """Normalize provider-specific severity or urgency values."""
        if value is None:
            return cls.UNKNOWN
        raw = str(value).strip().lower()
        if raw in {"critical", "crit", "p1", "sev1", "sev-1", "fatal", "emergency", "page", "1"}:
            return cls.CRITICAL
        if raw in {"high", "error", "err", "p2", "sev2", "sev-2", "major", "alarm", "alert", "failure", "failed", "2"}:
            return cls.HIGH
        if raw in {"warning", "warn", "medium", "moderate", "p3", "sev3", "sev-3", "degraded", "unstable", "3"}:
            return cls.WARNING
        if raw in {"info", "informational", "low", "ok", "normal", "p4", "p5", "sev4", "4", "5"}:
            return cls.INFO
        return cls.UNKNOWN


class WebhookEventStatus(StrEnum):
    """Normalized lifecycle state of an incoming webhook event."""

    FIRING = "firing"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"
    ANNOTATED = "annotated"
    FAILED = "failed"
    SUCCESS = "success"
    CHALLENGE = "challenge"
    INFO = "info"


@dataclass(slots=True)
class NormalizedWebhookEvent:
    """Platform-agnostic representation of a verified inbound webhook event."""

    provider: str
    """Provider key (e.g. 'sns', 'pagerduty', 'alertmanager', 'cloudevents', 'jenkins')."""

    event_type: str
    """Provider-specific event type (e.g. 'incident.triggered', 'alarm.state_change')."""

    incident_id: str
    """Stable identifier used to map related events into the same LangGraph thread."""

    title: str
    """Short human-readable alert/event title."""

    summary: str
    """Detailed description, alert message, or failure log excerpt."""

    severity: WebhookSeverity = WebhookSeverity.UNKNOWN
    """Normalized severity level."""

    status: WebhookEventStatus = WebhookEventStatus.FIRING
    """Normalized event lifecycle status."""

    service_name: str | None = None
    """Affected service, application, cluster, or pipeline name."""

    source_url: str | None = None
    """Deep link back to the originating incident, alarm, or build run."""

    caller_id: str = "webhook"
    """Caller identifier for IdentityMapper RBAC resolution."""

    dedup_key: str | None = None
    """Optional key for retry/storm deduplication. Defaults to provider:incident_id:event_type."""

    raw_payload: dict[str, Any] = field(default_factory=dict)
    """Unmodified webhook payload for full context inspection."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extracted structured labels, annotations, dimensions, or tags."""

    callback_url: str | None = None
    """Optional outbound webhook URL to notify when the agent run finishes."""

    reply_target: dict[str, Any] = field(default_factory=dict)
    """Provider-specific coordinates for bidirectional write-back (e.g. incident_id, repo/issue)."""

    should_trigger_run: bool = True
    """Whether this event should start an autonomous agent run."""

    ignore_reason: str | None = None
    """Reason why should_trigger_run is False (e.g. 'resolved event', 'handshake challenge')."""

    challenge_response: dict[str, Any] | None = None
    """Immediate JSON response payload for provider verification handshakes."""

    @property
    def effective_dedup_key(self) -> str:
        """Return the composite deduplication key for this event."""
        if self.dedup_key:
            return f"{self.provider}:{self.dedup_key}"
        return f"{self.provider}:{self.incident_id}:{self.event_type}:{self.status}"

    def to_incoming_message(self, custom_instructions: str | None = None) -> IncomingMessage:
        """Convert this webhook event into an OpsCloud IncomingMessage."""
        return IncomingMessage(
            text=self.format_agent_prompt(custom_instructions=custom_instructions),
            user_id=self.caller_id,
            thread_id=self.incident_id,
            channel_id=f"webhook:{self.provider}",
            platform=self.provider,
            raw_event=self.raw_payload,
            context={
                "event_type": self.event_type,
                "severity": str(self.severity),
                "status": str(self.status),
                "service_name": self.service_name,
                "source_url": self.source_url,
                "metadata": self.metadata,
                "reply_target": self.reply_target,
            },
        )

    def format_agent_prompt(self, custom_instructions: str | None = None) -> str:
        """Format a structured SRE/DevOps investigation prompt for the OpsCloud agent."""
        lines: list[str] = [
            f"[WEBHOOK ALERT] Provider: {self.provider.upper()} | Event: {self.event_type}",
            f"Title: {self.title}",
            f"Severity: {self.severity.value.upper()} | Status: {self.status.value.upper()}",
            f"Incident / Group ID: {self.incident_id}",
        ]
        if self.service_name:
            lines.append(f"Service / Component: {self.service_name}")
        if self.source_url:
            lines.append(f"Source URL: {self.source_url}")

        lines.append("\n### Alert / Event Summary")
        lines.append(self.summary.strip() if self.summary else "(No summary provided)")

        if self.metadata:
            lines.append("\n### Extracted Labels & Metadata")
            try:
                lines.append("```json\n" + json.dumps(self.metadata, indent=2, default=str) + "\n```")
            except Exception:
                lines.append(str(self.metadata))

        lines.append("\n### Required Action")
        if custom_instructions and custom_instructions.strip():
            lines.append(custom_instructions.strip())
        else:
            lines.append(
                "1. Investigate the root cause of this alert/event using your available cloud, Kubernetes, "
                "observability, and repository tools.\n"
                "2. Correlate recent deployments, configuration changes, logs, or metric anomalies related "
                "to the affected service/resource.\n"
                "3. Provide a concise Root Cause Analysis (RCA), blast radius assessment, and recommended "
                "remediation steps."
            )
        return "\n".join(lines)


@dataclass(slots=True)
class WebhookConfig:
    """Runtime configuration for webhook verification, deduplication, and write-back sinks.

    Hydrated through OpsCloud's canonical configuration pipeline
    (`env_vars.py` -> `manifest.py` -> `config.toml` -> `Settings`).
    """

    secret_env_prefix: str = "OPSCLOUD_WEBHOOK_SECRET_"
    default_secret: str | None = None
    provider_secrets: dict[str, str] = field(default_factory=dict)
    auto_confirm_sns: bool = True
    ignore_resolved: bool = True
    dedup_ttl_seconds: float = 300.0
    max_concurrent_runs: int = 5
    default_callback_url: str | None = None
    slack_webhook_url: str | None = None
    pagerduty_api_key: str | None = None
    pagerduty_from_email: str | None = None
    servicenow_instance_url: str | None = None
    servicenow_api_token: str | None = None
    github_token: str | None = None
    custom_prompt_instructions: str | None = None
    assistant_id: str = "opscloud"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> WebhookConfig:
        """Hydrate WebhookConfig from OpsCloud's canonical `Settings` object."""
        from opscloud.config.env_vars import classify_env_bool
        from opscloud.config.settings import Settings as SettingsCls, resolve_env_var

        cfg = settings if settings is not None else SettingsCls.from_env()

        provider_secrets: dict[str, str] = {}
        if cfg.webhook_pagerduty_secret:
            provider_secrets["pagerduty"] = cfg.webhook_pagerduty_secret
        if cfg.webhook_github_secret:
            provider_secrets["github"] = cfg.webhook_github_secret
        if cfg.webhook_gitlab_token:
            provider_secrets["gitlab"] = cfg.webhook_gitlab_token
        if cfg.webhook_jenkins_token:
            provider_secrets["jenkins"] = cfg.webhook_jenkins_token

        # Support legacy/alias env vars via canonical resolve_env_var
        dedup_ttl = float(cfg.webhook_dedupe_ttl_seconds)
        legacy_dedup = resolve_env_var("WEBHOOK_DEDUP_TTL")
        if legacy_dedup is not None:
            try:
                dedup_ttl = max(0.0, float(legacy_dedup))
            except ValueError:
                pass

        auto_confirm_sns = bool(cfg.webhook_sns_auto_confirm)
        legacy_sns_confirm = resolve_env_var("WEBHOOK_AUTO_CONFIRM_SNS")
        if legacy_sns_confirm is not None:
            parsed_bool = classify_env_bool(legacy_sns_confirm)
            if parsed_bool is not None:
                auto_confirm_sns = parsed_bool

        ignore_resolved = True
        raw_ignore_resolved = resolve_env_var("WEBHOOK_IGNORE_RESOLVED")
        if raw_ignore_resolved is not None:
            parsed_ignore = classify_env_bool(raw_ignore_resolved)
            if parsed_ignore is not None:
                ignore_resolved = parsed_ignore

        return cls(
            secret_env_prefix=resolve_env_var("WEBHOOK_SECRET_PREFIX") or "OPSCLOUD_WEBHOOK_SECRET_",
            default_secret=cfg.webhook_secret or None,
            provider_secrets=provider_secrets,
            auto_confirm_sns=auto_confirm_sns,
            ignore_resolved=ignore_resolved,
            dedup_ttl_seconds=dedup_ttl,
            max_concurrent_runs=max(1, int(cfg.webhook_max_concurrent_runs)),
            default_callback_url=cfg.webhook_callback_url or None,
            slack_webhook_url=(
                cfg.webhook_slack_url
                or resolve_env_var("SLACK_WEBHOOK_URL")
                or None
            ),
            pagerduty_api_key=(
                cfg.webhook_pagerduty_api_key
                or resolve_env_var("PAGERDUTY_API_KEY")
                or None
            ),
            pagerduty_from_email=(
                cfg.webhook_pagerduty_from_email
                or resolve_env_var("PAGERDUTY_FROM_EMAIL")
                or None
            ),
            servicenow_instance_url=(
                resolve_env_var("SERVICENOW_URL", fallback_names=("SERVICENOW_INSTANCE_URL",))
                or None
            ),
            servicenow_api_token=(
                resolve_env_var("SERVICENOW_TOKEN", fallback_names=("SERVICENOW_API_TOKEN",))
                or None
            ),
            github_token=(
                cfg.webhook_github_token
                or resolve_env_var("GITHUB_TOKEN")
                or None
            ),
            custom_prompt_instructions=resolve_env_var("WEBHOOK_INSTRUCTIONS") or None,
            assistant_id=resolve_env_var("SERVER_ASSISTANT_ID") or cfg.assistant_id or "opscloud",
        )

    @classmethod
    def from_env(cls) -> WebhookConfig:
        """Load WebhookConfig through OpsCloud's canonical Settings resolution pipeline."""
        from opscloud.config.settings import Settings as SettingsCls

        return cls.from_settings(SettingsCls.from_env())

    def get_provider_secret(self, provider: str) -> str | None:
        """Resolve the webhook signing secret for a specific provider.

        Checks in order:
        1. Canonical provider secret from `Settings` (`webhooks.<provider>_secret` / `webhooks.<provider>_token`)
        2. Provider-specific env var (`OPSCLOUD_WEBHOOK_SECRET_<PROVIDER>` / `OPSCLOUD_WEBHOOK_<PROVIDER>_SECRET`)
        3. Default webhook secret (`webhooks.secret` / `OPSCLOUD_WEBHOOK_SECRET`)
        """
        from opscloud.config.settings import resolve_env_var

        norm = provider.lower().strip()
        if norm in self.provider_secrets and self.provider_secrets[norm]:
            return self.provider_secrets[norm]

        provider_upper = norm.upper().replace("-", "_")
        prefix_clean = self.secret_env_prefix
        if prefix_clean.startswith("OPSCLOUD_"):
            prefix_clean = prefix_clean[len("OPSCLOUD_") :]
        dynamic_secret = resolve_env_var(
            f"{prefix_clean}{provider_upper}",
            fallback_names=(
                f"{self.secret_env_prefix}{provider_upper}",
                f"OPSCLOUD_WEBHOOK_{provider_upper}_SECRET",
                f"OPSCLOUD_WEBHOOK_{provider_upper}_TOKEN",
            ),
        )
        return dynamic_secret or self.default_secret
