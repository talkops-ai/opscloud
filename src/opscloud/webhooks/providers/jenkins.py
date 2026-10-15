"""Jenkins CI/CD build & pipeline notification webhook adapter."""

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
from opscloud.webhooks.providers.base import WebhookProviderAdapter

_ACTIONABLE_BUILD_STATUSES = frozenset(
    {"FAILURE", "FAILED", "UNSTABLE", "ABORTED", "REGRESSION", "ERROR"}
)


class JenkinsWebhookAdapter(WebhookProviderAdapter):
    """Adapter for Jenkins Notification Plugin and Pipeline `httpRequest` webhooks."""

    name = "jenkins"
    aliases = ("ci", "jenkins-ci")
    description = "Jenkins CI/CD pipeline & build failure webhooks"

    def parse(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        config: WebhookConfig,
    ) -> NormalizedWebhookEvent:
        data = self.decode_json_body(raw_body)

        build_obj: dict[str, Any] = data.get("build") if isinstance(data.get("build"), dict) else data
        job_name = str(
            data.get("name")
            or data.get("job_name")
            or data.get("job")
            or build_obj.get("job_name")
            or "jenkins-job"
        )
        build_number = build_obj.get("number") or data.get("build_number") or data.get("number") or "latest"
        phase = str(build_obj.get("phase") or data.get("phase") or "COMPLETED").upper()
        status_str = str(
            build_obj.get("status")
            or data.get("status")
            or data.get("result")
            or "FAILURE"
        ).upper()
        full_url = (
            build_obj.get("full_url")
            or build_obj.get("url")
            or data.get("build_url")
            or data.get("url")
        )

        scm_info = build_obj.get("scm") if isinstance(build_obj.get("scm"), dict) else {}
        branch = scm_info.get("branch") or data.get("branch") or data.get("git_branch")
        commit = scm_info.get("commit") or data.get("commit") or data.get("git_commit")
        log_snippet = build_obj.get("log") or data.get("log") or data.get("error") or data.get("message")

        is_failed = status_str in _ACTIONABLE_BUILD_STATUSES
        is_in_progress = phase in {"STARTED", "QUEUED"} and status_str not in _ACTIONABLE_BUILD_STATUSES
        should_run = is_failed or not config.ignore_resolved
        if is_in_progress and config.ignore_resolved:
            should_run = False

        ignore_reason = None
        if not should_run:
            ignore_reason = f"jenkins_build_{phase.lower()}_{status_str.lower()}"

        summary_parts = [
            f"Jenkins Job: {job_name} #{build_number}",
            f"Phase: {phase} | Status: {status_str}",
        ]
        if branch or commit:
            summary_parts.append(f"Branch: {branch or 'unknown'} | Commit: {commit or 'unknown'}")
        if log_snippet:
            summary_parts.append(f"Build Log / Failure Details:\n{log_snippet}")
        elif build_obj:
            summary_parts.append(json.dumps(build_obj, indent=2, default=str))

        return NormalizedWebhookEvent(
            provider=self.name,
            event_type=f"jenkins.build.{status_str.lower()}",
            incident_id=f"jenkins:{job_name}:{build_number}",
            dedup_key=f"jenkins:{job_name}:{build_number}:{phase}:{status_str}",
            title=f"Jenkins Build {status_str}: {job_name} #{build_number}",
            summary="\n".join(summary_parts),
            severity=WebhookSeverity.HIGH if is_failed else WebhookSeverity.INFO,
            status=WebhookEventStatus.FAILED if is_failed else WebhookEventStatus.SUCCESS,
            service_name=job_name,
            source_url=str(full_url) if full_url else None,
            raw_payload=data,
            metadata={
                "job_name": job_name,
                "build_number": build_number,
                "phase": phase,
                "status": status_str,
                "branch": branch,
                "commit": commit,
                "parameters": build_obj.get("parameters") or data.get("parameters"),
            },
            should_trigger_run=should_run,
            ignore_reason=ignore_reason,
        )
