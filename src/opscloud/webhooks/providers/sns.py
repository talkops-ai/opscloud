"""AWS SNS, CloudWatch Alarms, and Amazon EventBridge webhook adapter."""

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
from opscloud.webhooks.security import is_valid_sns_subscribe_url


class SNSWebhookAdapter(WebhookProviderAdapter):
    """Adapter for AWS SNS HTTP/HTTPS notifications (CloudWatch Alarms, EventBridge, Health)."""

    name = "sns"
    aliases = ("cloudwatch", "eventbridge", "aws-sns", "aws")
    description = "AWS SNS, CloudWatch Alarms, and EventBridge event notifications"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)
        lower_headers = {k.lower(): v for k, v in headers.items()}

        msg_type = (
            data.get("Type")
            or lower_headers.get("x-amz-sns-message-type")
            or ""
        ).strip()

        topic_arn = str(data.get("TopicArn") or lower_headers.get("x-amz-sns-topic-arn") or "sns:unknown")
        message_id = str(data.get("MessageId") or lower_headers.get("x-amz-sns-message-id") or "sns-msg")

        # 1. Handle SNS SubscriptionConfirmation handshake
        if msg_type == "SubscriptionConfirmation":
            subscribe_url = str(data.get("SubscribeURL") or "").strip()
            if not is_valid_sns_subscribe_url(subscribe_url):
                raise WebhookParseError(
                    f"Invalid or untrusted SNS SubscribeURL: {subscribe_url!r}"
                )
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type="sns.subscription_confirmation",
                incident_id=topic_arn,
                title=f"SNS Subscription Confirmation ({topic_arn})",
                summary=f"Auto-confirming SNS topic subscription for {topic_arn}",
                severity=WebhookSeverity.INFO,
                status=WebhookEventStatus.CHALLENGE,
                raw_payload=data,
                metadata={"topic_arn": topic_arn, "subscribe_url": subscribe_url},
                should_trigger_run=False,
                ignore_reason="sns_subscription_confirmation",
                challenge_response={
                    "status": "subscription_confirmed" if config.auto_confirm_sns else "subscription_pending",
                    "topic_arn": topic_arn,
                    "subscribe_url": subscribe_url,
                },
            )

        if msg_type == "UnsubscribeConfirmation":
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type="sns.unsubscribe_confirmation",
                incident_id=topic_arn,
                title=f"SNS Unsubscribe Confirmation ({topic_arn})",
                summary="SNS topic unsubscribed",
                severity=WebhookSeverity.INFO,
                status=WebhookEventStatus.INFO,
                raw_payload=data,
                should_trigger_run=False,
                ignore_reason="sns_unsubscribe_confirmation",
            )

        # 2. Unwrap inner Message (JSON or plain string)
        raw_message = data.get("Message")
        inner: dict[str, Any] | None = None
        if isinstance(raw_message, str):
            try:
                parsed_inner = json.loads(raw_message)
                if isinstance(parsed_inner, dict):
                    inner = parsed_inner
            except json.JSONDecodeError:
                inner = None
        elif isinstance(raw_message, dict):
            inner = raw_message

        # 3. Direct EventBridge JSON (when sent directly without SNS envelope)
        if inner is None and "detail-type" in data and "source" in data:
            inner = data

        # 4. CloudWatch Alarm payload detection
        if inner and "AlarmName" in inner and "NewStateValue" in inner:
            return self._parse_cloudwatch_alarm(inner, outer=data, topic_arn=topic_arn, config=config)

        # 5. EventBridge event detection
        if inner and "detail-type" in inner and "source" in inner:
            return self._parse_eventbridge(inner, outer=data, topic_arn=topic_arn, config=config)

        # 6. Generic SNS Notification
        subject = str(data.get("Subject") or f"SNS Notification from {topic_arn.split(':')[-1]}")
        summary_text = (
            json.dumps(inner, indent=2)
            if inner is not None
            else str(raw_message or data.get("Message") or "")
        )
        return NormalizedWebhookEvent(
            provider=self.name,
            event_type="sns.notification",
            incident_id=f"{topic_arn}:{subject}",
            dedup_key=message_id,
            title=subject,
            summary=summary_text,
            severity=WebhookSeverity.HIGH,
            status=WebhookEventStatus.FIRING,
            service_name=topic_arn.split(":")[-1] if ":" in topic_arn else topic_arn,
            raw_payload=data,
            metadata={"topic_arn": topic_arn, "message_id": message_id},
            should_trigger_run=True,
        )

    def _parse_cloudwatch_alarm(
        self,
        alarm: dict[str, Any],
        *,
        outer: dict[str, Any],
        topic_arn: str,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        alarm_name = str(alarm.get("AlarmName") or "CloudWatchAlarm")
        new_state = str(alarm.get("NewStateValue") or "ALARM").upper()
        reason = str(alarm.get("NewStateReason") or alarm.get("AlarmDescription") or "")
        region = str(alarm.get("Region") or "")
        account_id = str(alarm.get("AWSAccountId") or "")
        trigger = alarm.get("Trigger") if isinstance(alarm.get("Trigger"), dict) else {}

        is_resolved = new_state == "OK"
        status = WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING
        severity = WebhookSeverity.INFO if is_resolved else WebhookSeverity.CRITICAL
        should_run = not (is_resolved and config.ignore_resolved)

        namespace = str(trigger.get("Namespace") or "")
        metric_name = str(trigger.get("MetricName") or "")
        dimensions = trigger.get("Dimensions") or []

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"cloudwatch.alarm.{new_state.lower()}",
            incident_id=f"cw:{account_id}:{region}:{alarm_name}",
            dedup_key=f"cw:{alarm_name}:{new_state}:{alarm.get('StateChangeTime', '')}",
            title=f"CloudWatch Alarm [{new_state}]: {alarm_name}",
            summary=reason,
            severity=severity,
            status=status,
            service_name=namespace or alarm_name,
            raw_payload=outer,
            metadata={
                "alarm_name": alarm_name,
                "alarm_arn": alarm.get("AlarmArn"),
                "state": new_state,
                "previous_state": alarm.get("OldStateValue"),
                "aws_account_id": account_id,
                "region": region,
                "metric_namespace": namespace,
                "metric_name": metric_name,
                "dimensions": dimensions,
                "topic_arn": topic_arn,
            },
            should_trigger_run=should_run,
            ignore_reason="cloudwatch_alarm_ok" if not should_run else None,
        )

    def _parse_eventbridge(
        self,
        event: dict[str, Any],
        *,
        outer: dict[str, Any],
        topic_arn: str,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        source = str(event.get("source") or "aws.events")
        detail_type = str(event.get("detail-type") or "Event")
        event_id = str(event.get("id") or "eb-event")
        account = str(event.get("account") or "")
        region = str(event.get("region") or "")
        resources = event.get("resources") or []
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {"raw": event.get("detail")}

        state_val = str(detail.get("state") or detail.get("status") or "").upper()
        is_resolved = state_val in {"OK", "RESOLVED", "CLOSED", "SUCCEEDED"}
        should_run = not (is_resolved and config.ignore_resolved)

        resource_key = str(resources[0]) if resources else f"{source}:{detail_type}"
        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"eventbridge.{source}",
            incident_id=f"eb:{account}:{region}:{resource_key}",
            dedup_key=event_id,
            title=f"EventBridge [{source}]: {detail_type}",
            summary=json.dumps(detail, indent=2, default=str),
            severity=WebhookSeverity.INFO if is_resolved else WebhookSeverity.HIGH,
            status=WebhookEventStatus.RESOLVED if is_resolved else WebhookEventStatus.FIRING,
            service_name=source,
            raw_payload=outer,
            metadata={
                "source": source,
                "detail_type": detail_type,
                "aws_account_id": account,
                "region": region,
                "resources": resources,
                "topic_arn": topic_arn,
            },
            should_trigger_run=should_run,
            ignore_reason="eventbridge_resolved" if not should_run else None,
        )
