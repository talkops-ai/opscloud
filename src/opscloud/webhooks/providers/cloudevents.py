"""CNCF CloudEvents v1.0 webhook adapter (Argo Events, Knative, Flux CD, Tekton, Keptn)."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from opscloud.webhooks.models import (
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookEventStatus,
    WebhookSeverity,
)
from opscloud.webhooks.providers.base import WebhookParseError, WebhookProviderAdapter


class CloudEventsWebhookAdapter(WebhookProviderAdapter):
    """Adapter supporting both Structured and Binary CNCF CloudEvents v1.0 HTTP bindings."""

    name = "cloudevents"
    aliases = ("cloudevent", "argo-events", "flux", "tekton", "knative")
    description = "CNCF CloudEvents v1.0 standard webhook receiver (structured & binary modes)"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        lower_headers = {k.lower(): v for k, v in headers.items()}

        # 1. Abuse protection / WebHook validation handshake (CloudEvents HTTP 1.0 OPTIONS/POST validation)
        origin = lower_headers.get("webhook-request-origin")
        if origin and not raw_body.strip():
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type="cloudevents.handshake",
                incident_id=f"ce-handshake:{origin}",
                title="CloudEvents Webhook Validation",
                summary=f"Webhook validation request from {origin}",
                severity=WebhookSeverity.INFO,
                status=WebhookEventStatus.CHALLENGE,
                should_trigger_run=False,
                ignore_reason="cloudevents_validation_handshake",
                challenge_response={"status": "ok", "allowed_origin": origin},
            )

        body_dict = self.decode_json_body(raw_body)

        # 2. Determine Binary Mode (`ce-type`, `ce-source` headers) vs Structured Mode (`specversion` in JSON)
        is_binary = "ce-type" in lower_headers and "ce-source" in lower_headers
        if is_binary:
            specversion = lower_headers.get("ce-specversion", "1.0")
            ce_id = lower_headers.get("ce-id", "ce-id")
            ce_source = lower_headers.get("ce-source", "unknown-source")
            ce_type = lower_headers.get("ce-type", "cloudevent")
            ce_subject = lower_headers.get("ce-subject")
            ce_time = lower_headers.get("ce-time")
            event_data: Any = body_dict
        else:
            if "type" not in body_dict or "source" not in body_dict:
                raise WebhookParseError(
                    "Invalid CloudEvents payload: missing required 'type' and 'source' attributes"
                )
            specversion = str(body_dict.get("specversion") or "1.0")
            ce_id = str(body_dict.get("id") or "ce-id")
            ce_source = str(body_dict.get("source") or "unknown-source")
            ce_type = str(body_dict.get("type") or "cloudevent")
            ce_subject = body_dict.get("subject")
            ce_time = body_dict.get("time")
            event_data = body_dict.get("data", body_dict)

        data_dict = event_data if isinstance(event_data, dict) else {"value": event_data}
        severity_raw = (
            data_dict.get("severity")
            or body_dict.get("severity")
            or lower_headers.get("ce-severity")
        )
        severity = WebhookSeverity.from_raw(severity_raw)
        if severity == WebhookSeverity.UNKNOWN:
            if any(tok in ce_type.lower() for tok in ("fail", "error", "alert", "crash", "degraded")):
                severity = WebhookSeverity.HIGH
            else:
                severity = WebhookSeverity.WARNING

        status_raw = str(data_dict.get("status") or data_dict.get("state") or "").lower()
        is_resolved = status_raw in {"resolved", "succeeded", "healthy", "ok"} and "fail" not in ce_type.lower()
        should_run = not (is_resolved and config.ignore_resolved)

        thread_anchor = f"{ce_source}:{ce_subject}" if ce_subject else f"{ce_source}:{ce_type}"
        summary_text = (
            str(data_dict.get("message") or data_dict.get("summary") or "")
            + "\n"
            + json.dumps(data_dict, indent=2, default=str)
        ).strip()

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=ce_type,
            incident_id=thread_anchor,
            dedup_key=f"{ce_source}:{ce_id}",
            title=f"CloudEvent [{ce_type}] from {ce_source}",
            summary=summary_text,
            severity=severity,
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=str(ce_subject or ce_source),
            raw_payload=body_dict,
            metadata={
                "specversion": specversion,
                "ce_id": ce_id,
                "ce_source": ce_source,
                "ce_type": ce_type,
                "ce_subject": ce_subject,
                "ce_time": ce_time,
            },
            should_trigger_run=should_run,
            ignore_reason="cloudevent_resolved" if not should_run else None,
        )
