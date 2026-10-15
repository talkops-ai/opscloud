"""Prometheus Alertmanager v4 & Grafana Unified Alerting webhook adapter (CNCF Standard)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opscloud.webhooks.models import (
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookEventStatus,
    WebhookSeverity,
)
from opscloud.webhooks.providers.base import WebhookParseError, WebhookProviderAdapter, as_dict


class AlertmanagerWebhookAdapter(WebhookProviderAdapter):
    """Adapter for Prometheus Alertmanager v4, Grafana Alerting, VictoriaMetrics, Mimir, and Loki."""

    name = "alertmanager"
    aliases = ("prometheus", "grafana", "mimir", "loki", "vmalert")
    description = "CNCF Prometheus Alertmanager v4 & Grafana Unified Alerting webhooks"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        alerts = data.get("alerts")
        if not isinstance(alerts, list) and "commonLabels" not in data and "status" not in data:
            raise WebhookParseError("Invalid Alertmanager payload: missing 'alerts' or 'status'")

        alerts_list = [a for a in (alerts or []) if isinstance(a, dict)]
        status_raw = str(data.get("status") or "firing").strip().lower()
        is_resolved = status_raw == "resolved"

        common_labels = as_dict(data.get("commonLabels"))
        common_annotations = as_dict(data.get("commonAnnotations"))
        group_labels = as_dict(data.get("groupLabels"))

        # Fall back to first alert's labels/annotations if commonLabels is empty
        first_alert = alerts_list[0] if alerts_list else {}
        first_labels = as_dict(first_alert.get("labels"))
        first_annotations = as_dict(first_alert.get("annotations"))

        merged_labels = {**first_labels, **common_labels}
        merged_annotations = {**first_annotations, **common_annotations}

        alert_name = str(
            merged_labels.get("alertname")
            or group_labels.get("alertname")
            or data.get("title")
            or "PrometheusAlert"
        )
        service_name = (
            merged_labels.get("service")
            or merged_labels.get("job")
            or merged_labels.get("app")
            or merged_labels.get("namespace")
        )
        severity_raw = merged_labels.get("severity") or merged_labels.get("priority")
        severity = WebhookSeverity.from_raw(severity_raw)
        if severity == WebhookSeverity.UNKNOWN:
            severity = WebhookSeverity.INFO if is_resolved else WebhookSeverity.HIGH

        group_key = str(
            data.get("groupKey")
            or first_alert.get("fingerprint")
            or f"am:{alert_name}:{service_name or 'default'}"
        )

        # Build concise summary of all alerts in the group
        summary_lines: list[str] = []
        top_summary = (
            merged_annotations.get("summary")
            or merged_annotations.get("description")
            or merged_annotations.get("message")
            or data.get("message")
        )
        if top_summary:
            summary_lines.append(str(top_summary))

        for idx, alert in enumerate(alerts_list[:10], start=1):
            a_status = str(alert.get("status") or status_raw)
            a_labels = as_dict(alert.get("labels"))
            a_ann = as_dict(alert.get("annotations"))
            a_desc = a_ann.get("description") or a_ann.get("summary") or ""
            target = (
                a_labels.get("pod")
                or a_labels.get("instance")
                or a_labels.get("node")
                or a_labels.get("deployment")
                or a_labels.get("alertname")
                or f"alert-{idx}"
            )
            summary_lines.append(f"- [{a_status.upper()}] {target}: {a_desc}")

        source_url = (
            first_alert.get("generatorURL")
            or first_alert.get("silenceURL")
            or data.get("externalURL")
        )

        should_run = not (is_resolved and config.ignore_resolved)
        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"alertmanager.{status_raw}",
            incident_id=group_key,
            dedup_key=f"{group_key}:{status_raw}:{len(alerts_list)}",
            title=f"[{status_raw.upper()}] {alert_name} ({len(alerts_list) or 1} alert(s))",
            summary="\n".join(summary_lines) if summary_lines else f"Alert {alert_name} is {status_raw}",
            severity=severity,
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=str(service_name) if service_name else None,
            source_url=str(source_url) if source_url else None,
            raw_payload=data,
            metadata={
                "alertname": alert_name,
                "receiver": data.get("receiver"),
                "alert_count": len(alerts_list),
                "labels": merged_labels,
                "annotations": merged_annotations,
            },
            should_trigger_run=should_run,
            ignore_reason="alertmanager_resolved" if not should_run else None,
        )
