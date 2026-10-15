"""GitHub Actions, Deployments, and Issue/PR webhook adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opscloud.webhooks.models import (
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookEventStatus,
    WebhookSeverity,
)
from opscloud.webhooks.providers.base import WebhookProviderAdapter


class GitHubWebhookAdapter(WebhookProviderAdapter):
    """Adapter for GitHub webhooks (`workflow_run`, `check_run`, `deployment_status`, `issue_comment`)."""

    name = "github"
    aliases = ("gh", "github-actions")
    description = "GitHub Actions workflow failures, deployment status, and @opscloud issue/PR mentions"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)
        lower_headers = {k.lower(): v for k, v in headers.items()}

        gh_event = lower_headers.get("x-github-event", "webhook").strip().lower()
        delivery_id = lower_headers.get("x-github-delivery", "")
        action = str(data.get("action") or "").strip().lower()

        repo_obj = data.get("repository") if isinstance(data.get("repository"), dict) else {}
        repo_name = str(repo_obj.get("full_name") or repo_obj.get("name") or "unknown/repo")
        sender_obj = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        sender = str(sender_obj.get("login") or "github")

        # 1. Ping handshake
        if gh_event == "ping":
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type="github.ping",
                incident_id=f"gh:{repo_name}:ping",
                title=f"GitHub Webhook Ping ({repo_name})",
                summary=str(data.get("zen") or "GitHub webhook configured"),
                severity=WebhookSeverity.INFO,
                status=WebhookEventStatus.CHALLENGE,
                service_name=repo_name,
                caller_id=sender,
                raw_payload=data,
                should_trigger_run=False,
                ignore_reason="github_ping",
                challenge_response={"status": "pong", "repository": repo_name},
            )

        # 2. GitHub Actions workflow_run or check_run
        if gh_event in {"workflow_run", "check_run", "check_suite"}:
            run_obj = (
                data.get(gh_event)
                if isinstance(data.get(gh_event), dict)
                else {}
            )
            run_id = str(run_obj.get("id") or "run")
            name = str(run_obj.get("name") or run_obj.get("head_branch") or gh_event)
            conclusion = str(run_obj.get("conclusion") or "").lower()
            html_url = run_obj.get("html_url")
            branch = run_obj.get("head_branch")
            head_sha = run_obj.get("head_sha")

            is_failed = conclusion in {"failure", "timed_out", "action_required", "startup_failure"}
            should_run = (action == "completed" and is_failed) or not config.ignore_resolved

            return NormalizedWebhookEvent(
                provider=self.name,
                event_type=f"github.{gh_event}.{conclusion or action}",
                incident_id=f"gh:{repo_name}:{gh_event}:{run_id}",
                dedup_key=delivery_id or f"gh:{repo_name}:{run_id}:{action}:{conclusion}",
                title=f"GitHub Actions [{conclusion.upper() or action.upper()}]: {repo_name} / {name}",
                summary=(
                    f"Repository: {repo_name}\n"
                    f"Workflow / Check: {name} (ID: {run_id})\n"
                    f"Branch: {branch} | Commit: {head_sha}\n"
                    f"Conclusion: {conclusion or action}\n"
                    f"URL: {html_url or 'N/A'}"
                ),
                severity=WebhookSeverity.HIGH if is_failed else WebhookSeverity.INFO,
                status=WebhookEventStatus.FAILED if is_failed else WebhookEventStatus.SUCCESS,
                service_name=repo_name,
                source_url=str(html_url) if html_url else None,
                caller_id=sender,
                raw_payload=data,
                metadata={
                    "repository": repo_name,
                    "run_id": run_id,
                    "workflow_name": name,
                    "branch": branch,
                    "head_sha": head_sha,
                    "conclusion": conclusion,
                },
                reply_target={"repository": repo_name, "run_id": run_id, "head_sha": head_sha},
                should_trigger_run=should_run,
                ignore_reason=None if should_run else f"github_{gh_event}_{action}_{conclusion}",
            )

        # 3. Deployment status failure
        if gh_event == "deployment_status":
            dep_status = data.get("deployment_status") if isinstance(data.get("deployment_status"), dict) else {}
            deployment = data.get("deployment") if isinstance(data.get("deployment"), dict) else {}
            state = str(dep_status.get("state") or "").lower()
            env_name = str(dep_status.get("environment") or deployment.get("environment") or "production")
            desc = str(dep_status.get("description") or "")
            target_url = dep_status.get("target_url") or dep_status.get("environment_url")
            dep_id = str(deployment.get("id") or "deploy")

            is_failed = state in {"failure", "error"}
            should_run = is_failed or not config.ignore_resolved
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type=f"github.deployment_status.{state}",
                incident_id=f"gh:{repo_name}:deploy:{dep_id}",
                dedup_key=delivery_id or f"gh:{repo_name}:deploy:{dep_id}:{state}",
                title=f"GitHub Deployment [{state.upper()}] in {repo_name} ({env_name})",
                summary=f"Deployment #{dep_id} to '{env_name}' entered state '{state}'.\nDescription: {desc}",
                severity=WebhookSeverity.CRITICAL if is_failed else WebhookSeverity.INFO,
                status=WebhookEventStatus.FAILED if is_failed else WebhookEventStatus.INFO,
                service_name=repo_name,
                source_url=str(target_url) if target_url else None,
                caller_id=sender,
                raw_payload=data,
                metadata={"repository": repo_name, "environment": env_name, "state": state, "deployment_id": dep_id},
                reply_target={"repository": repo_name, "deployment_id": dep_id},
                should_trigger_run=should_run,
                ignore_reason=None if should_run else f"github_deployment_{state}",
            )

        # 4. Issue / PR comments mentioning @opscloud or /opscloud
        issue_obj = data.get("issue") or data.get("pull_request") or {}
        if isinstance(issue_obj, dict) and issue_obj:
            issue_num = issue_obj.get("number") or "0"
            title = str(issue_obj.get("title") or f"Issue #{issue_num}")
            comment_obj = data.get("comment") if isinstance(data.get("comment"), dict) else {}
            body_text = str(comment_obj.get("body") or issue_obj.get("body") or "")
            html_url = comment_obj.get("html_url") or issue_obj.get("html_url")

            mentioned = any(tok in body_text.lower() for tok in ("@opscloud", "/opscloud"))
            should_run = mentioned or gh_event == "issues" and action == "opened"
            return NormalizedWebhookEvent(
                provider=self.name,
                event_type=f"github.{gh_event}.{action}",
                incident_id=f"gh:{repo_name}:issue:{issue_num}",
                dedup_key=delivery_id or f"gh:{repo_name}:issue:{issue_num}:{comment_obj.get('id', action)}",
                title=f"GitHub {repo_name} #{issue_num}: {title}",
                summary=body_text or title,
                severity=WebhookSeverity.WARNING,
                status=WebhookEventStatus.FIRING,
                service_name=repo_name,
                source_url=str(html_url) if html_url else None,
                caller_id=sender,
                raw_payload=data,
                metadata={"repository": repo_name, "issue_number": issue_num, "action": action},
                reply_target={"repository": repo_name, "issue_number": issue_num},
                should_trigger_run=should_run,
                ignore_reason=None if should_run else "github_comment_not_mentioned",
            )

        # Fallback for any other GitHub event
        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"github.{gh_event}",
            incident_id=f"gh:{repo_name}:{gh_event}",
            dedup_key=delivery_id or f"gh:{repo_name}:{gh_event}:{action}",
            title=f"GitHub Event [{gh_event}] on {repo_name}",
            summary=f"GitHub event '{gh_event}' (action='{action}') triggered by {sender}",
            severity=WebhookSeverity.INFO,
            status=WebhookEventStatus.INFO,
            service_name=repo_name,
            caller_id=sender,
            raw_payload=data,
            should_trigger_run=not config.ignore_resolved,
            ignore_reason="github_unhandled_event" if config.ignore_resolved else None,
        )
