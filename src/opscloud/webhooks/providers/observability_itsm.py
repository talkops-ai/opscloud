"""Datadog, ServiceNow, and Generic (Azure Monitor, Opsgenie, Jira, Argo CD) webhook adapters."""

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
from opscloud.webhooks.providers.base import WebhookProviderAdapter, as_dict


class DatadogWebhookAdapter(WebhookProviderAdapter):
    """Adapter for Datadog Monitor and Synthetics alert webhooks."""

    name = "datadog"
    aliases = ("dd",)
    description = "Datadog Monitor & Synthetics alert webhooks"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        alert_id = str(data.get("alert_id") or data.get("id") or "dd-alert")
        title = str(data.get("alert_title") or data.get("title") or f"Datadog Alert {alert_id}")
        transition = str(data.get("alert_transition") or data.get(" alert_status") or "Triggered").strip()
        metric = data.get("alert_metric") or data.get("metric")
        scope = data.get("alert_scope") or data.get("scope")
        link = data.get("link") or data.get("url")
        priority = str(data.get("priority") or data.get("alert_priority") or "P2")
        tags = data.get("tags")
        body = str(data.get("body") or data.get("event_msg") or data.get("message") or title)

        is_recovered = transition.lower() in {"recovered", "ok", "resolved", "no data"}
        should_run = not (is_recovered and config.ignore_resolved)

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"datadog.monitor.{transition.lower()}",
            incident_id=f"dd:{alert_id}",
            dedup_key=f"dd:{alert_id}:{transition}:{data.get('last_updated', '')}",
            title=f"Datadog [{transition}]: {title}",
            summary=body,
            severity=WebhookSeverity.INFO if is_recovered else WebhookSeverity.from_raw(priority),
            status=WebhookEventStatus.RESOLVED if is_recovered else WebhookEventStatus.FIRING,
            service_name=str(scope) if scope else None,
            source_url=str(link) if link else None,
            raw_payload=data,
            metadata={
                "alert_id": alert_id,
                "transition": transition,
                "metric": metric,
                "scope": scope,
                "priority": priority,
                "tags": tags,
            },
            should_trigger_run=should_run,
            ignore_reason="datadog_recovered" if not should_run else None,
        )


class ServiceNowWebhookAdapter(WebhookProviderAdapter):
    """Adapter for ServiceNow Incident outbound business rule webhooks."""

    name = "servicenow"
    aliases = ("snow", "itsm")
    description = "ServiceNow ITSM Incident webhooks with work-note write-back"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        sys_id = str(data.get("sys_id") or data.get("id") or "snow-incident")
        number = str(data.get("number") or data.get("incident_number") or sys_id)
        short_desc = str(data.get("short_description") or data.get("title") or f"ServiceNow Incident {number}")
        desc = str(data.get("description") or data.get("comments_and_work_notes") or short_desc)
        priority = data.get("priority") or data.get("urgency") or "2"
        state = str(data.get("state") or data.get("incident_state") or "New")
        cmdb_ci = data.get("cmdb_ci") or data.get("service") or data.get("business_service")
        url = data.get("url") or data.get("link")

        is_resolved = state.lower() in {"resolved", "closed", "cancelled", "6", "7", "8"}
        should_run = not (is_resolved and config.ignore_resolved)

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"servicenow.incident.{state.lower()}",
            incident_id=f"snow:{number}",
            dedup_key=f"snow:{sys_id}:{state}:{data.get('sys_updated_on', '')}",
            title=f"ServiceNow [{number}]: {short_desc}",
            summary=desc,
            severity=WebhookSeverity.INFO if is_resolved else WebhookSeverity.from_raw(priority),
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=str(cmdb_ci) if cmdb_ci else None,
            source_url=str(url) if url else None,
            raw_payload=data,
            metadata={
                "sys_id": sys_id,
                "number": number,
                "state": state,
                "priority": priority,
                "cmdb_ci": cmdb_ci,
                "assignment_group": data.get("assignment_group"),
            },
            reply_target={"sys_id": sys_id, "number": number},
            should_trigger_run=should_run,
            ignore_reason="servicenow_incident_resolved" if not should_run else None,
        )


class GenericWebhookAdapter(WebhookProviderAdapter):
    """Universal adapter for Azure Monitor Common Alert Schema, Opsgenie, Jira, Argo CD, and custom JSON webhooks."""

    name = "generic"
    aliases = ("custom", "azure-monitor", "opsgenie", "jira", "argocd", "gitlab")
    description = "Universal JSON webhook adapter (Azure Monitor, Opsgenie, Jira, Argo CD, custom pipelines)"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        # 1. Detect Azure Monitor Common Alert Schema (`schemaId == "azureMonitorCommonAlertSchema"`)
        if data.get("schemaId") == "azureMonitorCommonAlertSchema" and isinstance(data.get("data"), dict):
            return self._parse_azure_monitor(data, config=config)

        # 2. Extract common fields from arbitrary JSON webhook payloads
        alert_obj = as_dict(data.get("alert"), fallback=data)
        issue_obj = as_dict(data.get("issue"))
        issue_fields = as_dict(issue_obj.get("fields"))

        incident_id = str(
            data.get("thread_id")
            or data.get("incident_id")
            or alert_obj.get("alertId")
            or alert_obj.get("id")
            or issue_obj.get("key")
            or data.get("id")
            or "generic-event"
        )
        event_type = str(
            data.get("event_type")
            or data.get("action")
            or data.get("webhookEvent")
            or data.get("type")
            or "webhook.event"
        )
        title = str(
            data.get("title")
            or alert_obj.get("message")
            or issue_fields.get("summary")
            or data.get("subject")
            or data.get("prompt")
            or f"Webhook Event ({incident_id})"
        )
        summary = str(
            data.get("summary")
            or data.get("description")
            or data.get("prompt")
            or data.get("message")
            or alert_obj.get("description")
            or issue_fields.get("description")
            or json.dumps(data, indent=2, default=str)
        )
        issue_priority = as_dict(issue_fields.get("priority"))
        severity_raw = (
            data.get("severity")
            or alert_obj.get("priority")
            or issue_priority.get("name")
        )
        severity = WebhookSeverity.from_raw(severity_raw)
        if severity == WebhookSeverity.UNKNOWN:
            severity = WebhookSeverity.HIGH

        status_raw = str(data.get("status") or data.get("state") or "firing").lower()
        is_resolved = status_raw in {"resolved", "closed", "ok", "recovered"}
        should_run = not (is_resolved and config.ignore_resolved)

        callback_url = data.get("callback_url") or data.get("webhook")
        service_name = data.get("service") or data.get("app") or data.get("project")

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=event_type,
            incident_id=incident_id,
            title=title[:200],
            summary=summary,
            severity=severity,
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=str(service_name) if service_name else None,
            source_url=str(data.get("url") or data.get("source_url")) if (data.get("url") or data.get("source_url")) else None,
            caller_id=str(data.get("user_id") or data.get("caller") or "webhook"),
            raw_payload=data,
            metadata={k: v for k, v in data.items() if k not in {"raw_payload", "description", "summary"}},
            callback_url=str(callback_url) if callback_url else None,
            should_trigger_run=should_run,
            ignore_reason="generic_event_resolved" if not should_run else None,
        )

    def _parse_azure_monitor(
        self,
        payload: dict[str, Any],
        *,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data_obj = as_dict(payload.get("data"))
        essentials = as_dict(data_obj.get("essentials"))
        context = as_dict(data_obj.get("alertContext"))

        alert_id = str(essentials.get("alertId") or "azure-alert")
        alert_rule = str(essentials.get("alertRule") or "AzureMonitorAlert")
        severity_raw = str(essentials.get("severity") or "Sev2")
        monitor_condition = str(essentials.get("monitorCondition") or "Fired")
        signal_type = str(essentials.get("signalType") or "Metric")
        targets = essentials.get("alertTargetIDs") or []
        description = str(essentials.get("description") or "")

        is_resolved = monitor_condition.lower() == "resolved"
        should_run = not (is_resolved and config.ignore_resolved)

        return NormalizedWebhookEvent(
            provider="azure-monitor",
            event_type=f"azure.monitor.{monitor_condition.lower()}",
            incident_id=alert_id,
            dedup_key=f"{alert_id}:{monitor_condition}:{essentials.get('firedDateTime', '')}",
            title=f"Azure Monitor [{monitor_condition}] ({severity_raw}): {alert_rule}",
            summary=f"{description}\n\nTarget Resources: {targets}\nAlert Context:\n{json.dumps(context, indent=2, default=str)}".strip(),
            severity=WebhookSeverity.INFO if is_resolved else WebhookSeverity.from_raw(severity_raw),
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=str(targets[0]).split("/")[-1] if targets else alert_rule,
            raw_payload=payload,
            metadata={
                "alert_id": alert_id,
                "alert_rule": alert_rule,
                "severity": severity_raw,
                "monitor_condition": monitor_condition,
                "signal_type": signal_type,
                "target_ids": targets,
            },
            should_trigger_run=should_run,
            ignore_reason="azure_monitor_resolved" if not should_run else None,
        )
