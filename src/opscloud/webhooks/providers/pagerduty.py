"""PagerDuty v3 (and legacy v2) webhook adapter with loop-safe note detection."""

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
from opscloud.webhooks.providers.base import WebhookParseError, WebhookProviderAdapter, as_dict

_ACTIONABLE_PD_EVENTS = frozenset(
    {
        "incident.triggered",
        "incident.escalated",
        "incident.reopened",
        "incident.annotated",
    }
)


class PagerDutyWebhookAdapter(WebhookProviderAdapter):
    """Adapter for PagerDuty v3 Generic Webhooks (`event.event_type`, `event.data`)."""

    name = "pagerduty"
    aliases = ("pd",)
    description = "PagerDuty v3 Incident Webhooks with bidirectional incident note write-back"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        # PagerDuty v3 schema: {"event": {"id": ..., "event_type": ..., "data": {...}}}
        event_obj = data.get("event")
        if isinstance(event_obj, dict):
            return self._parse_v3_event(event_obj, outer=data, config=config)

        # Legacy PagerDuty v2 fallback: {"messages": [{"event": ..., "incident": {...}}]}
        messages = data.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            return self._parse_v2_event(messages[0], outer=data, config=config)

        raise WebhookParseError("Unrecognized PagerDuty webhook schema: missing 'event' object")

    def _parse_v3_event(
        self,
        event: dict[str, Any],
        *,
        outer: dict[str, Any],
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        event_id = str(event.get("id") or "")
        event_type = str(event.get("event_type") or "incident.triggered").strip()
        event_data = as_dict(event.get("data"))

        # For incident.annotated, PagerDuty places the incident under data.incident and note in data.content
        if event_type == "incident.annotated":
            incident_ref = as_dict(event_data.get("incident"), fallback=event_data)
            incident_id = str(incident_ref.get("id") or event_data.get("id") or "pd-incident")
            incident_title = str(incident_ref.get("summary") or incident_ref.get("title") or f"Incident {incident_id}")
            note_content = str(event_data.get("content") or "").strip()
            html_url = incident_ref.get("html_url")
            service_obj = as_dict(incident_ref.get("service"))
            service_name = str(service_obj.get("summary") or "") or None

            # Loop prevention: ignore annotations written by OpsCloud itself
            is_self_note = note_content.startswith("[OpsCloud") or "OpsCloud Automated RCA" in note_content
            should_run = bool(note_content) and not is_self_note
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type=event_type,
                incident_id=incident_id,
                dedup_key=event_id or f"{incident_id}:annotated:{hash(note_content)}",
                title=f"PagerDuty Note on {incident_title}",
                summary=f"Follow-up responder note added to incident {incident_id}:\n{note_content}",
                severity=WebhookSeverity.HIGH,
                status=WebhookEventStatus.ANNOTATED,
                service_name=service_name,
                source_url=str(html_url) if html_url else None,
                raw_payload=outer,
                metadata={"incident_id": incident_id, "event_id": event_id},
                reply_target={"incident_id": incident_id},
                should_trigger_run=should_run,
                ignore_reason="self_authored_note" if is_self_note else None,
            )

        incident_id = str(event_data.get("id") or "pd-incident")
        incident_number = event_data.get("number")
        title = str(event_data.get("title") or f"PagerDuty Incident {incident_id}")
        urgency = str(event_data.get("urgency") or "high").lower()
        status_str = str(event_data.get("status") or "triggered").lower()
        html_url = event_data.get("html_url")

        service_obj = as_dict(event_data.get("service"))
        service_name = str(service_obj.get("summary") or service_obj.get("name") or "") or None

        priority_obj = as_dict(event_data.get("priority"))
        priority_name = str(priority_obj.get("summary") or priority_obj.get("name") or "")

        severity = (
            WebhookSeverity.from_raw(priority_name)
            if priority_name
            else (WebhookSeverity.CRITICAL if urgency == "high" else WebhookSeverity.WARNING)
        )
        if severity == WebhookSeverity.UNKNOWN:
            severity = WebhookSeverity.CRITICAL if urgency == "high" else WebhookSeverity.WARNING

        if event_type == "incident.resolved" or status_str == "resolved":
            norm_status = WebhookEventStatus.RESOLVED
        elif event_type == "incident.acknowledged" or status_str == "acknowledged":
            norm_status = WebhookEventStatus.ACKNOWLEDGED
        else:
            norm_status = WebhookEventStatus.FIRING

        should_run = event_type in _ACTIONABLE_PD_EVENTS and norm_status == WebhookEventStatus.FIRING
        ignore_reason = None if should_run else f"non_actionable_event:{event_type}"

        body_details = event_data.get("body") or event_data.get("custom_details") or {}
        summary_parts = [f"Incident #{incident_number}: {title}" if incident_number else title]
        if body_details:
            summary_parts.append(
                json.dumps(body_details, indent=2, default=str)
                if isinstance(body_details, dict)
                else str(body_details)
            )

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=event_type,
            incident_id=incident_id,
            dedup_key=event_id or f"{incident_id}:{event_type}",
            title=title,
            summary="\n\n".join(summary_parts),
            severity=severity,
            status=norm_status,
            service_name=service_name,
            source_url=str(html_url) if html_url else None,
            raw_payload=outer,
            metadata={
                "incident_id": incident_id,
                "incident_number": incident_number,
                "urgency": urgency,
                "priority": priority_name or None,
                "service": service_name,
                "event_id": event_id,
            },
            reply_target={"incident_id": incident_id},
            should_trigger_run=should_run,
            ignore_reason=ignore_reason,
        )

    def _parse_v2_event(
        self,
        msg: dict[str, Any],
        *,
        outer: dict[str, Any],
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        event_type = str(msg.get("event") or "incident.trigger")
        incident = as_dict(msg.get("incident"))
        incident_id = str(incident.get("id") or "pd-v2-incident")
        title = str(incident.get("title") or incident.get("summary") or f"Incident {incident_id}")
        urgency = str(incident.get("urgency") or "high")
        html_url = incident.get("html_url")
        service = as_dict(incident.get("service"))
        service_name = str(service.get("name") or service.get("summary") or "") or None

        is_trigger = "trigger" in event_type or "escalate" in event_type
        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=event_type,
            incident_id=incident_id,
            dedup_key=str(msg.get("id") or f"{incident_id}:{event_type}"),
            title=title,
            summary=str(incident.get("description") or title),
            severity=WebhookSeverity.CRITICAL if urgency == "high" else WebhookSeverity.WARNING,
            status=WebhookEventStatus.FIRING if is_trigger else WebhookEventStatus.RESOLVED,
            service_name=service_name,
            source_url=str(html_url) if html_url else None,
            raw_payload=outer,
            metadata={"incident_id": incident_id, "urgency": urgency, "service": service_name},
            reply_target={"incident_id": incident_id},
            should_trigger_run=is_trigger or not config.ignore_resolved,
            ignore_reason=None if is_trigger else f"non_trigger_v2_event:{event_type}",
        )
