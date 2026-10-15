"""Outbound write-back sinks and SNS handshake confirmation for OpsCloud webhooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from opscloud.integrations.content_filter import filter_internal_content
from opscloud.integrations.hooks import dispatch_hook_fire_and_forget
from opscloud.utils.logger import get_logger
from opscloud.webhooks.models import NormalizedWebhookEvent, WebhookConfig
from opscloud.webhooks.security import is_valid_sns_subscribe_url

logger = get_logger(__name__)

_PAGERDUTY_NOTES_MAX_CHARS = 20000
_SLACK_MAX_CHARS = 3500


@dataclass(slots=True)
class WebhookRunResult:
    """Outcome of a background LangGraph run triggered by a webhook."""

    run_id: str
    thread_id: str
    status: str
    analysis_text: str
    error: str | None = None


async def confirm_sns_subscription(
    subscribe_url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Confirm an AWS SNS topic subscription by visiting its validated SubscribeURL."""
    if not is_valid_sns_subscribe_url(subscribe_url):
        logger.warning("Refusing to confirm invalid SNS SubscribeURL: %r", subscribe_url)
        return False

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        resp = await client.get(subscribe_url)
        resp.raise_for_status()
        logger.info("Confirmed AWS SNS subscription via %s", subscribe_url)
        return True
    except Exception as exc:
        logger.warning("Failed to confirm AWS SNS subscription: %s", exc)
        return False
    finally:
        if owns_client:
            await client.aclose()


async def dispatch_completion_sinks(
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    config: WebhookConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, bool]:
    """Deliver the agent's root-cause analysis to all configured outbound sinks."""
    cleaned_analysis = filter_internal_content(result.analysis_text).strip()
    if not cleaned_analysis and result.error:
        cleaned_analysis = f"Run failed with error: {result.error}"

    # 1. Dispatch local OpsCloud hook event
    dispatch_hook_fire_and_forget(
        "webhook.completed",
        {
            "provider": event.provider,
            "event_type": event.event_type,
            "incident_id": event.incident_id,
            "thread_id": result.thread_id,
            "run_id": result.run_id,
            "status": result.status,
            "title": event.title,
            "severity": str(event.severity),
            "analysis": cleaned_analysis,
            "error": result.error,
        },
    )

    outcomes: dict[str, bool] = {}
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)

    try:
        # 2. PagerDuty Incident Note write-back
        pd_incident_id = event.reply_target.get("incident_id") if event.provider == "pagerduty" else None
        if pd_incident_id and config.pagerduty_api_key and config.pagerduty_from_email:
            outcomes["pagerduty"] = await _post_pagerduty_note(
                client,
                incident_id=str(pd_incident_id),
                analysis=cleaned_analysis,
                api_key=config.pagerduty_api_key,
                from_email=config.pagerduty_from_email,
            )

        # 3. ServiceNow Work Note write-back
        snow_sys_id = event.reply_target.get("sys_id") if event.provider == "servicenow" else None
        if snow_sys_id and config.servicenow_instance_url and config.servicenow_api_token:
            outcomes["servicenow"] = await _post_servicenow_work_note(
                client,
                instance_url=config.servicenow_instance_url,
                sys_id=str(snow_sys_id),
                analysis=cleaned_analysis,
                api_token=config.servicenow_api_token,
            )

        # 4. GitHub Issue / PR Comment write-back
        gh_repo = event.reply_target.get("repository") if event.provider == "github" else None
        gh_issue = event.reply_target.get("issue_number") if event.provider == "github" else None
        if gh_repo and gh_issue and config.github_token:
            outcomes["github"] = await _post_github_comment(
                client,
                repository=str(gh_repo),
                issue_number=int(gh_issue),
                analysis=cleaned_analysis,
                token=config.github_token,
            )

        # 5. Slack Incoming Webhook notification
        if config.slack_webhook_url:
            outcomes["slack"] = await _post_slack_notification(
                client,
                webhook_url=config.slack_webhook_url,
                event=event,
                result=result,
                analysis=cleaned_analysis,
            )

        # 6. Generic Outbound Callback URL
        callback_url = event.callback_url or config.default_callback_url
        if callback_url:
            outcomes["callback"] = await _post_callback_webhook(
                client,
                callback_url=callback_url,
                event=event,
                result=result,
                analysis=cleaned_analysis,
            )
    finally:
        if owns_client:
            await client.aclose()

    if outcomes:
        logger.info(
            "Webhook outbound sinks dispatched | provider=%s incident_id=%s thread_id=%s outcomes=%s",
            event.provider,
            event.incident_id,
            result.thread_id,
            outcomes,
        )
    else:
        logger.info(
            "Webhook run finalized | provider=%s incident_id=%s thread_id=%s (no external write-back sinks configured)",
            event.provider,
            event.incident_id,
            result.thread_id,
        )

    return outcomes


async def _post_pagerduty_note(
    client: httpx.AsyncClient,
    *,
    incident_id: str,
    analysis: str,
    api_key: str,
    from_email: str,
) -> bool:
    note_body = f"[OpsCloud Automated RCA]\n{analysis}"[:_PAGERDUTY_NOTES_MAX_CHARS]
    try:
        resp = await client.post(
            f"https://api.pagerduty.com/incidents/{incident_id}/notes",
            headers={
                "Authorization": f"Token token={api_key}",
                "From": from_email,
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
            },
            json={"note": {"content": note_body}},
        )
        resp.raise_for_status()
        logger.info("Posted OpsCloud RCA note to PagerDuty incident %s", incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post PagerDuty note for incident %s: %s", incident_id, exc)
        return False


async def _post_servicenow_work_note(
    client: httpx.AsyncClient,
    *,
    instance_url: str,
    sys_id: str,
    analysis: str,
    api_token: str,
) -> bool:
    base_url = instance_url.rstrip("/")
    try:
        resp = await client.patch(
            f"{base_url}/api/now/table/incident/{sys_id}",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"work_notes": f"[OpsCloud Automated RCA]\n{analysis}"},
        )
        resp.raise_for_status()
        logger.info("Posted OpsCloud work note to ServiceNow incident %s", sys_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post ServiceNow work note for %s: %s", sys_id, exc)
        return False


async def _post_github_comment(
    client: httpx.AsyncClient,
    *,
    repository: str,
    issue_number: int,
    analysis: str,
    token: str,
) -> bool:
    try:
        resp = await client.post(
            f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": f"### [OpsCloud Automated Analysis]\n\n{analysis}"},
        )
        resp.raise_for_status()
        logger.info("Posted OpsCloud analysis comment to GitHub %s#%d", repository, issue_number)
        return True
    except Exception as exc:
        logger.warning("Failed to post GitHub comment to %s#%d: %s", repository, issue_number, exc)
        return False


async def _post_slack_notification(
    client: httpx.AsyncClient,
    *,
    webhook_url: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
) -> bool:
    truncated = analysis[:_SLACK_MAX_CHARS]
    header = (
        f"*[OpsCloud Automated RCA]* `{event.provider.upper()}` | "
        f"*{event.title}* (`{event.severity.value.upper()}`)\n"
        f"Thread: `{result.thread_id}` | Status: `{result.status}`"
    )
    if event.source_url:
        header += f" | <{event.source_url}|View Source>"

    try:
        resp = await client.post(
            webhook_url,
            json={"text": f"{header}\n\n{truncated}"},
        )
        resp.raise_for_status()
        logger.info("Posted OpsCloud RCA notification to Slack webhook for incident %s", event.incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post Slack webhook notification: %s", exc)
        return False


async def _post_callback_webhook(
    client: httpx.AsyncClient,
    *,
    callback_url: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
) -> bool:
    payload: dict[str, Any] = {
        "provider": event.provider,
        "event_type": event.event_type,
        "incident_id": event.incident_id,
        "thread_id": result.thread_id,
        "run_id": result.run_id,
        "status": result.status,
        "title": event.title,
        "severity": str(event.severity),
        "service_name": event.service_name,
        "source_url": event.source_url,
        "analysis": analysis,
        "error": result.error,
    }
    try:
        resp = await client.post(callback_url, json=payload)
        resp.raise_for_status()
        logger.info("Posted OpsCloud RCA payload to callback URL %s for incident %s", callback_url, event.incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post outbound callback webhook to %s: %s", callback_url, exc)
        return False
