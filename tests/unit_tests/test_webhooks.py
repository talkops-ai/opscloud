"""Comprehensive unit tests for OpsCloud Central Webhook Server across all providers and flows."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from opscloud.cli.main import main as cli_main
from opscloud.server import create_webhooks_app, generate_langgraph_json
from opscloud.webhooks import (
    DeduplicationCache,
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookDispatcher,
    WebhookEventStatus,
    WebhookRunResult,
    WebhookSeverity,
    confirm_sns_subscription,
    create_default_registry,
    dispatch_completion_sinks,
    is_valid_sns_subscribe_url,
    verify_bearer_or_header_token,
    verify_hmac_sha256,
    verify_pagerduty_signature,
    verify_request_signature,
)


# ── 1. Security, Signatures, SSRF & Deduplication Tests ──────────────────────


def test_verify_hmac_sha256_and_github_signature() -> None:
    body = b'{"action":"completed","workflow_run":{"id":123,"conclusion":"failure"}}'
    secret = "gh-super-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_hmac_sha256(body, secret, f"sha256={digest}") is True
    assert verify_hmac_sha256(body, secret, "sha256=deadbeef") is False
    assert verify_hmac_sha256(body, "", f"sha256={digest}") is False
    assert verify_hmac_sha256(body, secret, None) is False

    headers = {"X-Hub-Signature-256": f"sha256={digest}"}
    assert verify_request_signature("github", headers, body, secret) is True
    assert verify_request_signature("github", headers, body, "wrong-secret") is False


def test_verify_pagerduty_v3_signature() -> None:
    body = b'{"event":{"id":"evt-1","event_type":"incident.triggered"}}'
    secret = "pd-webhook-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"v1=0000000000,v1={digest},v0=legacy"

    assert verify_pagerduty_signature(body, secret, header) is True
    assert verify_pagerduty_signature(body, "wrong", header) is False
    assert verify_request_signature("pagerduty", {"X-PagerDuty-Signature": header}, body, secret) is True


def test_verify_bearer_and_custom_header_tokens() -> None:
    secret = "jenkins-pipeline-token-99"
    assert verify_bearer_or_header_token({"Authorization": f"Bearer {secret}"}, secret) is True
    assert verify_bearer_or_header_token({"X-Jenkins-Token": secret}, secret) is True
    assert verify_bearer_or_header_token({"X-Webhook-Token": "wrong"}, secret) is False


def test_sns_subscribe_url_ssrf_validation() -> None:
    assert is_valid_sns_subscribe_url("https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc") is True
    assert is_valid_sns_subscribe_url("https://sns.cn-north-1.amazonaws.com.cn/?Action=ConfirmSubscription") is True
    # Reject non-HTTPS, localhost, metadata service, and arbitrary domains
    assert is_valid_sns_subscribe_url("http://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription") is False
    assert is_valid_sns_subscribe_url("https://169.254.169.254/latest/meta-data/") is False
    assert is_valid_sns_subscribe_url("https://localhost:8000/confirm") is False
    assert is_valid_sns_subscribe_url("https://evil-amazonaws.com/confirm") is False


def test_deduplication_cache_ttl_and_eviction() -> None:
    cache = DeduplicationCache(ttl_seconds=30.0, max_entries=3)
    assert cache.check_and_record("k1", now=100.0) is False
    assert cache.check_and_record("k1", now=110.0) is True
    # Expired after 30s
    assert cache.check_and_record("k1", now=131.0) is False

    # Capacity eviction
    cache.check_and_record("k2", now=132.0)
    cache.check_and_record("k3", now=133.0)
    cache.check_and_record("k4", now=134.0)  # Evicts k1
    assert cache.check_and_record("k1", now=135.0) is False


# ── 2. Individual Provider Parsing Flow Tests ────────────────────────────────


def test_sns_subscription_confirmation_and_cloudwatch_alarm_flows() -> None:
    registry = create_default_registry()
    sns = registry.get("sns")
    assert sns is not None
    cfg = WebhookConfig()

    # Flow A: SubscriptionConfirmation handshake
    sub_payload = {
        "Type": "SubscriptionConfirmation",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:opscloud-alerts",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=xyz",
    }
    sub_event = sns.parse({}, json.dumps(sub_payload).encode(), cfg)
    assert sub_event.status == WebhookEventStatus.CHALLENGE
    assert sub_event.should_trigger_run is False
    assert sub_event.challenge_response is not None
    assert sub_event.challenge_response["status"] == "subscription_confirmed"

    # Flow B: CloudWatch Alarm in ALARM state wrapped inside SNS Notification
    cw_inner = {
        "AlarmName": "HighPodRestartRate-prod",
        "AlarmDescription": "Pod restarts > 5 in 5m",
        "AWSAccountId": "123456789012",
        "Region": "us-east-1",
        "NewStateValue": "ALARM",
        "OldStateValue": "OK",
        "NewStateReason": "Threshold Crossed: 1 datapoint [8.0] was greater than the threshold (5.0).",
        "StateChangeTime": "2026-10-14T12:00:00.000+0000",
        "Trigger": {
            "MetricName": "pod_number_of_container_restarts",
            "Namespace": "ContainerInsights",
            "Dimensions": [{"name": "ClusterName", "value": "prod-eks"}],
        },
    }
    sns_cw_payload = {
        "Type": "Notification",
        "MessageId": "msg-cw-1",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:opscloud-alerts",
        "Subject": "ALARM: HighPodRestartRate-prod",
        "Message": json.dumps(cw_inner),
    }
    cw_event = sns.parse({}, json.dumps(sns_cw_payload).encode(), cfg)
    assert cw_event.should_trigger_run is True
    assert cw_event.severity == WebhookSeverity.CRITICAL
    assert cw_event.status == WebhookEventStatus.FIRING
    assert cw_event.incident_id == "cw:123456789012:us-east-1:HighPodRestartRate-prod"
    assert cw_event.metadata["metric_name"] == "pod_number_of_container_restarts"

    # Flow C: CloudWatch Alarm in OK state -> ignored by default
    cw_inner["NewStateValue"] = "OK"
    sns_cw_payload["Message"] = json.dumps(cw_inner)
    ok_event = sns.parse({}, json.dumps(sns_cw_payload).encode(), cfg)
    assert ok_event.status == WebhookEventStatus.RESOLVED
    assert ok_event.should_trigger_run is False

    # Flow D: EventBridge notification via alias "eventbridge"
    eb_adapter = registry.get("eventbridge")
    assert eb_adapter is not None
    assert eb_adapter is sns
    eb_payload = {
        "id": "eb-101",
        "source": "aws.ecs",
        "detail-type": "ECS Task State Change",
        "account": "123456789012",
        "region": "us-west-2",
        "resources": ["arn:aws:ecs:us-west-2:123456789012:task/prod/abc"],
        "detail": {"lastStatus": "STOPPED", "stoppedReason": "Essential container in task exited"},
    }
    eb_event = eb_adapter.parse({}, json.dumps(eb_payload).encode(), cfg)
    assert eb_event.should_trigger_run is True
    assert eb_event.event_type == "eventbridge.aws.ecs"
    assert "arn:aws:ecs" in eb_event.incident_id


def test_pagerduty_v3_incident_and_loop_prevention_flows() -> None:
    registry = create_default_registry()
    pd = registry.get("pagerduty")
    assert pd is not None
    cfg = WebhookConfig()

    # Flow A: incident.triggered
    triggered_payload = {
        "event": {
            "id": "01F8X7triggered",
            "event_type": "incident.triggered",
            "data": {
                "id": "Q123456789",
                "number": 42,
                "title": "Checkout API 5xx Error Rate > 10%",
                "urgency": "high",
                "status": "triggered",
                "html_url": "https://acme.pagerduty.com/incidents/Q123456789",
                "service": {"summary": "checkout-service"},
                "priority": {"summary": "P1"},
            },
        }
    }
    ev_trig = pd.parse({}, json.dumps(triggered_payload).encode(), cfg)
    assert ev_trig.should_trigger_run is True
    assert ev_trig.incident_id == "Q123456789"
    assert ev_trig.severity == WebhookSeverity.CRITICAL
    assert ev_trig.service_name == "checkout-service"
    assert ev_trig.reply_target == {"incident_id": "Q123456789"}

    # Flow B: Human responder adds a note (incident.annotated) -> same incident_id, triggers run
    human_note_payload = {
        "event": {
            "id": "01F8X7note1",
            "event_type": "incident.annotated",
            "data": {
                "id": "note-1",
                "content": "Seeing OOMKilled on checkout-redis pods in us-east-1, please check memory limits.",
                "incident": {
                    "id": "Q123456789",
                    "summary": "Checkout API 5xx Error Rate > 10%",
                    "service": {"summary": "checkout-service"},
                },
            },
        }
    }
    ev_note = pd.parse({}, json.dumps(human_note_payload).encode(), cfg)
    assert ev_note.should_trigger_run is True
    assert ev_note.incident_id == ev_trig.incident_id == "Q123456789"
    assert ev_note.status == WebhookEventStatus.ANNOTATED

    # Flow C: OpsCloud's own automated RCA note echoed back by PagerDuty -> MUST be ignored (loop guard)
    self_note_payload = {
        "event": {
            "id": "01F8X7note2",
            "event_type": "incident.annotated",
            "data": {
                "id": "note-2",
                "content": "[OpsCloud Automated RCA]\nRoot cause identified: Redis maxmemory reached.",
                "incident": {"id": "Q123456789"},
            },
        }
    }
    ev_self = pd.parse({}, json.dumps(self_note_payload).encode(), cfg)
    assert ev_self.should_trigger_run is False
    assert ev_self.ignore_reason == "self_authored_note"


def test_alertmanager_and_grafana_cncf_flows() -> None:
    registry = create_default_registry()
    am = registry.get("alertmanager")
    assert am is not None
    assert registry.get("prometheus") is am
    assert registry.get("grafana") is am
    cfg = WebhookConfig()

    am_payload = {
        "version": "4",
        "groupKey": '{}:{alertname="KubePodCrashLooping", namespace="payments"}',
        "status": "firing",
        "receiver": "opscloud-webhook",
        "groupLabels": {"alertname": "KubePodCrashLooping"},
        "commonLabels": {
            "alertname": "KubePodCrashLooping",
            "namespace": "payments",
            "service": "payment-worker",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "Pod payments/payment-worker-7f9d8b-x2k is crash looping",
        },
        "externalURL": "http://alertmanager.monitoring.svc:9093",
        "alerts": [
            {
                "status": "firing",
                "labels": {"pod": "payment-worker-7f9d8b-x2k", "severity": "critical"},
                "annotations": {"description": "Container restarted 7 times in the last 10m"},
                "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
                "fingerprint": "a1b2c3d4",
            }
        ],
    }
    ev = am.parse({}, json.dumps(am_payload).encode(), cfg)
    assert ev.should_trigger_run is True
    assert ev.severity == WebhookSeverity.CRITICAL
    assert ev.service_name == "payment-worker"
    assert "payment-worker-7f9d8b-x2k" in ev.summary
    assert ev.incident_id == '{}:{alertname="KubePodCrashLooping", namespace="payments"}'


def test_cloudevents_structured_and_binary_cncf_flows() -> None:
    registry = create_default_registry()
    ce = registry.get("cloudevents")
    assert ce is not None
    assert registry.get("argo-events") is ce
    assert registry.get("flux") is ce
    cfg = WebhookConfig()

    # Flow A: Structured CloudEvent (application/cloudevents+json)
    structured_body = {
        "specversion": "1.0",
        "id": "ce-9001",
        "source": "https://fluxcd.io/kustomize-controller",
        "type": "io.fluxcd.notification.reconciliation.failed",
        "subject": "Kustomization/prod-clusters",
        "data": {
            "severity": "error",
            "message": "Health check failed after 5m0s timeout for Deployment/ingress-nginx",
        },
    }
    ev_struct = ce.parse(
        {"Content-Type": "application/cloudevents+json"},
        json.dumps(structured_body).encode(),
        cfg,
    )
    assert ev_struct.should_trigger_run is True
    assert ev_struct.severity == WebhookSeverity.HIGH
    assert ev_struct.incident_id == "https://fluxcd.io/kustomize-controller:Kustomization/prod-clusters"

    # Flow B: Binary CloudEvent (ce-* headers + payload body)
    binary_headers = {
        "ce-specversion": "1.0",
        "ce-id": "tekton-run-77",
        "ce-source": "/apis/tekton.dev/v1/namespaces/ci/pipelineruns/deploy-prod",
        "ce-type": "dev.tekton.event.pipelinerun.failed.v1",
        "ce-subject": "deploy-prod",
    }
    binary_body = {"message": "Step 'terraform-apply' exited with code 1"}
    ev_bin = ce.parse(binary_headers, json.dumps(binary_body).encode(), cfg)
    assert ev_bin.should_trigger_run is True
    assert ev_bin.severity == WebhookSeverity.HIGH
    assert ev_bin.event_type == "dev.tekton.event.pipelinerun.failed.v1"
    assert ev_bin.service_name == "deploy-prod"


def test_jenkins_build_failure_and_success_flows() -> None:
    registry = create_default_registry()
    jk = registry.get("jenkins")
    assert jk is not None
    cfg = WebhookConfig()

    failed_build = {
        "name": "platform-terraform-apply",
        "build": {
            "number": 184,
            "phase": "COMPLETED",
            "status": "FAILURE",
            "full_url": "https://jenkins.internal/job/platform-terraform-apply/184/",
            "scm": {"branch": "origin/main", "commit": "8f2d1a9"},
            "log": "Error: creating EKS Node Group: AccessDeniedException",
        },
    }
    ev_fail = jk.parse({}, json.dumps(failed_build).encode(), cfg)
    assert ev_fail.should_trigger_run is True
    assert ev_fail.status == WebhookEventStatus.FAILED
    assert ev_fail.severity == WebhookSeverity.HIGH
    assert ev_fail.incident_id == "jenkins:platform-terraform-apply:184"
    assert "AccessDeniedException" in ev_fail.summary

    # Successful build is ignored
    failed_build["build"]["status"] = "SUCCESS"
    ev_ok = jk.parse({}, json.dumps(failed_build).encode(), cfg)
    assert ev_ok.should_trigger_run is False


def test_github_workflow_and_comment_flows() -> None:
    registry = create_default_registry()
    gh = registry.get("github")
    assert gh is not None
    cfg = WebhookConfig()

    # Flow A: Ping
    ping_ev = gh.parse({"X-GitHub-Event": "ping"}, b'{"zen":"Speak like a human","repository":{"full_name":"org/infra"}}', cfg)
    assert ping_ev.status == WebhookEventStatus.CHALLENGE
    assert ping_ev.should_trigger_run is False

    # Flow B: Failed workflow_run
    wf_payload = {
        "action": "completed",
        "repository": {"full_name": "org/infra"},
        "sender": {"login": "devops-bot"},
        "workflow_run": {
            "id": 555111,
            "name": "Deploy Production",
            "head_branch": "main",
            "head_sha": "deadbeef123",
            "conclusion": "failure",
            "html_url": "https://github.com/org/infra/actions/runs/555111",
        },
    }
    wf_ev = gh.parse({"X-GitHub-Event": "workflow_run"}, json.dumps(wf_payload).encode(), cfg)
    assert wf_ev.should_trigger_run is True
    assert wf_ev.status == WebhookEventStatus.FAILED
    assert wf_ev.incident_id == "gh:org/infra:workflow_run:555111"

    # Flow C: PR comment with @opscloud mention vs without mention
    comment_payload = {
        "action": "created",
        "repository": {"full_name": "org/infra"},
        "sender": {"login": "alice"},
        "issue": {"number": 88, "title": "Fix RDS parameter group"},
        "comment": {"id": 990, "body": "@opscloud please check why terraform plan failed on this PR"},
    }
    ev_mention = gh.parse({"X-GitHub-Event": "issue_comment"}, json.dumps(comment_payload).encode(), cfg)
    assert ev_mention.should_trigger_run is True
    assert ev_mention.reply_target == {"repository": "org/infra", "issue_number": 88}

    comment_payload["comment"]["body"] = "LGTM!"
    ev_no_mention = gh.parse({"X-GitHub-Event": "issue_comment"}, json.dumps(comment_payload).encode(), cfg)
    assert ev_no_mention.should_trigger_run is False


def test_datadog_servicenow_azure_monitor_and_generic_flows() -> None:
    registry = create_default_registry()
    cfg = WebhookConfig()

    # Datadog
    dd = registry.get("datadog")
    assert dd is not None
    dd_ev = dd.parse(
        {},
        json.dumps(
            {
                "alert_id": "dd-777",
                "alert_title": "High p99 Latency on API Gateway",
                "alert_transition": "Triggered",
                "alert_scope": "service:api-gw,env:prod",
                "priority": "P1",
                "body": "p99 latency exceeded 2.5s",
            }
        ).encode(),
        cfg,
    )
    assert dd_ev.should_trigger_run is True
    assert dd_ev.severity == WebhookSeverity.CRITICAL

    # ServiceNow
    snow = registry.get("servicenow")
    assert snow is not None
    snow_ev = snow.parse(
        {},
        json.dumps(
            {
                "sys_id": "a9f8d7c6",
                "number": "INC0045123",
                "short_description": "Production EKS cluster nodes NotReady",
                "priority": "1",
                "state": "In Progress",
                "cmdb_ci": "prod-eks-us-east-1",
            }
        ).encode(),
        cfg,
    )
    assert snow_ev.should_trigger_run is True
    assert snow_ev.reply_target == {"sys_id": "a9f8d7c6", "number": "INC0045123"}

    # Azure Monitor Common Alert Schema via generic / azure-monitor alias
    az = registry.get("azure-monitor")
    assert az is not None
    az_payload = {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertId": "/subscriptions/sub-1/providers/Microsoft.AlertsManagement/alerts/al-99",
                "alertRule": "AKS-Node-CPU-High",
                "severity": "Sev1",
                "monitorCondition": "Fired",
                "alertTargetIDs": ["/subscriptions/sub-1/resourcegroups/rg/providers/Microsoft.ContainerService/managedClusters/aks-prod"],
                "description": "Node CPU > 90% for 15 minutes",
            },
            "alertContext": {"metricName": "cpuUsagePercentage", "metricValue": 94.5},
        },
    }
    az_ev = az.parse({}, json.dumps(az_payload).encode(), cfg)
    assert az_ev.provider == "azure-monitor"
    assert az_ev.should_trigger_run is True
    assert az_ev.severity == WebhookSeverity.CRITICAL
    assert az_ev.service_name == "aks-prod"


# ── 3. Outbound Write-Back Sinks Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_completion_sinks_all_targets() -> None:
    recorded_requests: list[httpx.Request] = []

    async def _mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cfg = WebhookConfig(
            pagerduty_api_key="pd-key-123",
            pagerduty_from_email="sre-bot@company.com",
            slack_webhook_url="https://hooks.slack.com/services/T00/B00/X00",
            default_callback_url="https://callback.internal/webhook-done",
        )
        event = NormalizedWebhookEvent(
            provider="pagerduty",
            event_type="incident.triggered",
            incident_id="Q998877",
            title="Database Connection Pool Exhausted",
            summary="RDS max_connections reached",
            severity=WebhookSeverity.CRITICAL,
            reply_target={"incident_id": "Q998877"},
        )
        run_res = WebhookRunResult(
            run_id="run-111",
            thread_id="thread-222",
            status="success",
            analysis_text="## ACTIVE PLAN (LOCKED)\nRoot cause: leaky connection pool in billing-service v2.4.1.",
        )
        outcomes = await dispatch_completion_sinks(event, run_res, cfg, http_client=client)

    assert outcomes == {"pagerduty": True, "slack": True, "callback": True}
    assert len(recorded_requests) == 3

    # Verify PagerDuty note payload has internal plan markers stripped and loop-safe header included
    pd_req = next(r for r in recorded_requests if "api.pagerduty.com" in str(r.url))
    pd_body = json.loads(pd_req.content.decode())
    assert "[OpsCloud Automated RCA]" in pd_body["note"]["content"]
    assert "ACTIVE PLAN" not in pd_body["note"]["content"]
    assert "leaky connection pool" in pd_body["note"]["content"]


@pytest.mark.asyncio
async def test_confirm_sns_subscription_ssrf_and_success() -> None:
    visited: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(200, text="<ConfirmSubscriptionResponse/>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        ok = await confirm_sns_subscription(
            "https://sns.us-west-2.amazonaws.com/?Action=ConfirmSubscription&Token=123",
            http_client=client,
        )
        assert ok is True
        assert len(visited) == 1

        # SSRF attempt is blocked before any HTTP call
        blocked = await confirm_sns_subscription(
            "http://169.254.169.254/latest/meta-data/",
            http_client=client,
        )
        assert blocked is False
        assert len(visited) == 1


# ── 4. End-to-End Starlette ASGI App & Dispatcher Flow Tests ─────────────────


@pytest.mark.asyncio
async def test_webhooks_asgi_app_end_to_end_flows() -> None:
    executed_runs: list[tuple[NormalizedWebhookEvent, str]] = []

    async def _fake_executor(
        event: NormalizedWebhookEvent,
        lg_thread_id: str,
        delivery_id: str,
        config: WebhookConfig,
    ) -> WebhookRunResult:
        executed_runs.append((event, lg_thread_id))
        return WebhookRunResult(
            run_id=delivery_id,
            thread_id=lg_thread_id,
            status="success",
            analysis_text=f"Investigated {event.incident_id}: identified misconfigured security group.",
        )

    cfg = WebhookConfig(default_secret="shared-test-secret", dedup_ttl_seconds=60.0)
    dispatcher = WebhookDispatcher(config=cfg, run_executor=_fake_executor)
    asgi_app = create_webhooks_app(dispatcher=dispatcher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
    ) as client:
        # 1. GET /webhooks discovery endpoint
        resp_list = await client.get("/webhooks")
        assert resp_list.status_code == 200
        providers_data = resp_list.json()
        assert providers_data["status"] == "ok"
        provider_names = {p["provider"] for p in providers_data["providers"]}
        assert {"sns", "pagerduty", "alertmanager", "cloudevents", "jenkins", "github", "datadog", "servicenow", "generic"} <= provider_names

        # 2. Unauthorized POST rejected with 401
        pd_payload = {
            "event": {
                "id": "evt-pd-1",
                "event_type": "incident.triggered",
                "data": {"id": "INC-700", "title": "API Outage", "urgency": "high"},
            }
        }
        raw_pd = json.dumps(pd_payload).encode()
        resp_unauth = await client.post("/webhooks/pagerduty", content=raw_pd)
        assert resp_unauth.status_code == 401

        # 3. Authorized PagerDuty POST accepted with 202 and background run executed
        pd_sig = "v1=" + hmac.new(b"shared-test-secret", raw_pd, hashlib.sha256).hexdigest()
        resp_accept = await client.post(
            "/webhooks/pagerduty",
            content=raw_pd,
            headers={"X-PagerDuty-Signature": pd_sig},
        )
        assert resp_accept.status_code == 202
        accept_json = resp_accept.json()
        assert accept_json["status"] == "accepted"
        first_thread_id = accept_json["thread_id"]

        # 4. Duplicate retry of the exact same event is deduplicated
        resp_dup = await client.post(
            "/webhooks/pagerduty",
            content=raw_pd,
            headers={"X-PagerDuty-Signature": pd_sig},
        )
        assert resp_dup.status_code == 202
        assert resp_dup.json()["status"] == "deduplicated"

        # 5. Follow-up annotation on the same PagerDuty incident maps to the EXACT SAME LangGraph thread_id
        note_payload = {
            "event": {
                "id": "evt-pd-2",
                "event_type": "incident.annotated",
                "data": {
                    "id": "note-99",
                    "content": "Seeing 502s from ALB in eu-west-1",
                    "incident": {"id": "INC-700", "summary": "API Outage"},
                },
            }
        }
        raw_note = json.dumps(note_payload).encode()
        note_sig = "v1=" + hmac.new(b"shared-test-secret", raw_note, hashlib.sha256).hexdigest()
        resp_note = await client.post(
            "/webhooks/pagerduty",
            content=raw_note,
            headers={"X-PagerDuty-Signature": note_sig},
        )
        assert resp_note.status_code == 202
        assert resp_note.json()["status"] == "accepted"
        assert resp_note.json()["thread_id"] == first_thread_id

        await dispatcher.drain_background_tasks()
        assert len(executed_runs) == 2
        assert executed_runs[0][1] == executed_runs[1][1] == first_thread_id

        # 6. Verify /webhooks/deliveries endpoint
        resp_deliv = await client.get("/webhooks/deliveries")
        assert resp_deliv.status_code == 200
        assert len(resp_deliv.json()["deliveries"]) == 2


def test_generate_langgraph_json_mounts_webhooks_app(tmp_path: Path) -> None:
    target = generate_langgraph_json(tmp_path)
    config = json.loads(target.read_text(encoding="utf-8"))
    assert config["http"]["app"] == "./webhooks_app.py:app"
    assert (tmp_path / "webhooks_app.py").is_file()
    assert "opscloud.server.webhooks_app" in (tmp_path / "webhooks_app.py").read_text(encoding="utf-8")


def test_cli_serve_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["serve", "list", "--output-format", "json"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["command"] == "serve list"
    providers = {p["provider"] for p in parsed["data"]["providers"]}
    assert "sns" in providers
    assert "pagerduty" in providers
    assert "alertmanager" in providers
    assert "cloudevents" in providers
    assert "jenkins" in providers

    # Also test via `webhooks --list --json` alias
    exit_code2 = cli_main(["webhooks", "--list", "--json"])
    assert exit_code2 == 0
    captured2 = capsys.readouterr().out
    parsed2 = json.loads(captured2)
    assert parsed2["command"] == "serve list"
    assert len(parsed2["data"]["providers"]) >= 9


@pytest.mark.asyncio
async def test_default_langgraph_run_executor() -> None:
    from opscloud.webhooks.dispatcher import default_langgraph_run_executor

    mock_client = MagicMock()
    mock_client.threads.create = AsyncMock(return_value={"thread_id": "t-123"})
    mock_client.runs.create = AsyncMock(return_value={"run_id": "r-456"})
    mock_client.runs.join = AsyncMock(return_value=None)
    mock_client.runs.get = AsyncMock(return_value={"status": "success", "error": None})
    mock_client.threads.get_state = AsyncMock(
        return_value={
            "values": {
                "messages": [
                    {"role": "user", "content": "Investigate"},
                    {"role": "assistant", "content": "RCA: Memory limit on pod exceeded."},
                ]
            }
        }
    )

    event = NormalizedWebhookEvent(
        provider="alertmanager",
        event_type="alertmanager.firing",
        incident_id="grp-1",
        title="OOMKilled",
        summary="Container OOMKilled",
        severity=WebhookSeverity.CRITICAL,
    )
    cfg = WebhookConfig()

    with patch("langgraph_sdk.get_client", return_value=mock_client):
        res = await default_langgraph_run_executor(event, "t-123", "deliv-1", cfg)

    assert res.run_id == "r-456"
    assert res.thread_id == "t-123"
    assert res.status == "success"
    assert res.analysis_text == "RCA: Memory limit on pod exceeded."
    mock_client.threads.create.assert_awaited_once()
    mock_client.runs.create.assert_awaited_once()
    mock_client.runs.join.assert_awaited_once_with("t-123", "r-456")


def test_cli_serve_flags_after_subcommand() -> None:
    from opscloud.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--host", "0.0.0.0", "--port", "2024", "--approval-mode", "yolo", "--read-only"]
    )
    assert args.subcommand == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 2024
    assert args.approval_mode == "yolo"
    assert args.read_only is True

    mock_proc = MagicMock()
    mock_proc.port = 2024
    mock_proc.start.return_value = "http://127.0.0.1:2024"
    mock_proc.process = None

    with patch("opscloud.cli.commands.serve.ServerProcess", return_value=mock_proc) as mock_cls:
        exit_code = cli_main(
            ["serve", "--host", "0.0.0.0", "--port", "2024", "--approval-mode", "yolo"]
        )
        assert exit_code == 0
        server_cfg = mock_cls.call_args.args[0]
        assert server_cfg.approval_mode == "yolo"
        assert server_cfg.auto_approve is False
        mock_proc.start.assert_called_once()
        mock_proc.stop.assert_called_once()

    # Also test `--yolo` shorthand on `opscloud serve`
    with patch("opscloud.cli.commands.serve.ServerProcess", return_value=mock_proc) as mock_cls2:
        exit_code2 = cli_main(["serve", "--yolo"])
        assert exit_code2 == 0
        server_cfg2 = mock_cls2.call_args.args[0]
        assert server_cfg2.approval_mode == "yolo"


def test_yolo_mode_scope_and_resolution(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from opscloud.agent.factory import _should_interrupt_tool_call
    from opscloud.approval_mode import (
        ALL_APPROVAL_MODES,
        INTERACTIVE_APPROVAL_MODES,
        ApprovalMode,
        coerce_approval_mode,
        has_yolo_acknowledgement,
        next_approval_mode,
        save_yolo_acknowledgement,
    )
    from opscloud.cli.main import _resolve_approval_mode, build_parser
    from opscloud.security.approval_mode_source import ApprovalPolicyResolver

    # 1. Verify four canonical modes and three interactive modes
    assert ALL_APPROVAL_MODES == (
        ApprovalMode.MANUAL,
        ApprovalMode.SMART,
        ApprovalMode.AUTO,
        ApprovalMode.YOLO,
    )
    assert INTERACTIVE_APPROVAL_MODES == (
        ApprovalMode.MANUAL,
        ApprovalMode.SMART,
        ApprovalMode.AUTO,
    )

    # 2. coerce_approval_mode respects allow_yolo
    assert coerce_approval_mode("yolo", allow_yolo=True) == ApprovalMode.YOLO
    assert coerce_approval_mode("yolo", allow_yolo=False) == ApprovalMode.MANUAL
    assert coerce_approval_mode(ApprovalMode.YOLO, allow_yolo=False) == ApprovalMode.MANUAL

    # 3. Interactive mode switcher never cycles into YOLO and exits YOLO to MANUAL
    assert next_approval_mode(ApprovalMode.MANUAL) == ApprovalMode.AUTO
    assert next_approval_mode(ApprovalMode.AUTO) == ApprovalMode.SMART
    assert next_approval_mode(ApprovalMode.SMART) == ApprovalMode.MANUAL
    assert next_approval_mode(ApprovalMode.YOLO) == ApprovalMode.MANUAL

    # 4. CLI headless (-p) allows --yolo and --approval-mode yolo
    parser = build_parser()
    headless_args = parser.parse_args(["-p", "check cluster health", "--yolo"])
    assert _resolve_approval_mode(headless_args) == ApprovalMode.YOLO

    headless_mode_args = parser.parse_args(["-p", "check cluster health", "--approval-mode", "yolo"])
    assert _resolve_approval_mode(headless_mode_args) == ApprovalMode.YOLO

    # 5. Interactive TUI rejects --yolo and --approval-mode yolo with exit code 2
    code1 = cli_main(["--yolo"])
    assert code1 == 2
    err1 = capsys.readouterr().err
    assert "'yolo' approval mode is only supported in CLI headless mode" in err1

    code2 = cli_main(["--approval-mode", "yolo"])
    assert code2 == 2
    err2 = capsys.readouterr().err
    assert "'yolo' approval mode is only supported in CLI headless mode" in err2

    # 6. ApprovalPolicyResolver and _should_interrupt_tool_call handle YOLO
    assert ApprovalPolicyResolver.resolve_sync({"yolo": True}) == ApprovalMode.YOLO
    assert ApprovalPolicyResolver.resolve_sync({"approval_mode": "yolo"}) == ApprovalMode.YOLO
    assert (
        _should_interrupt_tool_call(
            {
                "state": {"approval_mode": "yolo"},
                "tool_call": {"id": "tc-1", "name": "execute", "args": {"command": "kubectl get pods"}},
            }
        )
        is False
    )

    # 7. YOLO acknowledgement persistence
    ack_file = tmp_path / "approval.json"
    assert has_yolo_acknowledgement(ack_file) is False
    assert save_yolo_acknowledgement(ack_file) is True
    assert has_yolo_acknowledgement(ack_file) is True


def test_webhook_config_routed_via_canonical_settings_and_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from opscloud.config.manifest import get_option
    from opscloud.config.settings import Settings

    # 1. Verify manifest options are registered under canonical keys
    sec_opt = get_option("security.approval_mode")
    assert sec_opt is not None
    assert sec_opt.choices == ("manual", "smart", "auto", "yolo")

    for key in (
        "webhooks.host",
        "webhooks.port",
        "webhooks.secret",
        "webhooks.pagerduty_secret",
        "webhooks.github_secret",
        "webhooks.gitlab_token",
        "webhooks.jenkins_token",
        "webhooks.dedupe_ttl_seconds",
        "webhooks.max_concurrent_runs",
        "webhooks.sns_auto_confirm",
        "webhooks.slack_url",
        "webhooks.pagerduty_api_key",
        "webhooks.pagerduty_from_email",
        "webhooks.github_token",
        "webhooks.callback_url",
    ):
        assert get_option(key) is not None, f"Missing manifest option: {key}"

    # 2. Verify Settings -> WebhookConfig.from_settings routing
    settings = Settings(
        webhook_host="127.0.0.1",
        webhook_port=9090,
        webhook_secret="global-secret-xyz",
        webhook_pagerduty_secret="pd-secret-111",
        webhook_github_secret="gh-secret-222",
        webhook_gitlab_token="gl-token-333",
        webhook_jenkins_token="jk-token-444",
        webhook_dedupe_ttl_seconds=180,
        webhook_max_concurrent_runs=8,
        webhook_sns_auto_confirm=False,
        webhook_slack_url="https://hooks.slack.com/services/T1/B1/X1",
        webhook_pagerduty_api_key="pd-api-key",
        webhook_pagerduty_from_email="ops@example.com",
        webhook_github_token="ghp_testtoken",
        webhook_callback_url="https://callback.example.com/hook",
    )
    cfg = WebhookConfig.from_settings(settings)
    assert cfg.default_secret == "global-secret-xyz"
    assert cfg.get_provider_secret("pagerduty") == "pd-secret-111"
    assert cfg.get_provider_secret("github") == "gh-secret-222"
    assert cfg.get_provider_secret("gitlab") == "gl-token-333"
    assert cfg.get_provider_secret("jenkins") == "jk-token-444"
    assert cfg.get_provider_secret("alertmanager") == "global-secret-xyz"
    assert cfg.dedup_ttl_seconds == 180.0
    assert cfg.max_concurrent_runs == 8
    assert cfg.auto_confirm_sns is False
    assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T1/B1/X1"
    assert cfg.pagerduty_api_key == "pd-api-key"
    assert cfg.pagerduty_from_email == "ops@example.com"
    assert cfg.github_token == "ghp_testtoken"
    assert cfg.default_callback_url == "https://callback.example.com/hook"


@pytest.mark.asyncio
async def test_webhook_logging_lifecycle_and_live_streaming(tmp_path: Path) -> None:
    import io
    from opscloud.cli.commands.serve import _drain_live_logs, _resolve_serve_log_level
    from opscloud.utils.logger import configure_logging
    from opscloud.webhooks.dispatcher import default_langgraph_run_executor

    # 1. Verify _resolve_serve_log_level defaults to INFO and respects --verbose / --log-level
    import argparse

    assert _resolve_serve_log_level(argparse.Namespace()) == "INFO"
    assert _resolve_serve_log_level(argparse.Namespace(verbose=True)) == "DEBUG"
    assert _resolve_serve_log_level(argparse.Namespace(log_level="WARNING")) == "WARNING"

    # 2. Verify live log draining helper streams new lines and respects quiet=True
    log_file = tmp_path / "webhook_live.log"
    log_file.write_text("[INFO] webhooks.dispatcher: [2026-10-14T10:00:00] Webhook received | provider=pagerduty\n", encoding="utf-8")
    buf = io.StringIO()
    with open(log_file, encoding="utf-8") as reader:
        _drain_live_logs(reader, stream=buf, quiet=False)
        assert "Webhook received | provider=pagerduty" in buf.getvalue()

        # Quiet mode should suppress output
        log_file.write_text(
            log_file.read_text(encoding="utf-8") + "[INFO] suppressed line\n",
            encoding="utf-8",
        )
        _drain_live_logs(reader, stream=buf, quiet=True)
        assert "suppressed line" not in buf.getvalue()

    # 3. Configure file logging to tmp_path and verify full webhook + executor + sink logging
    test_log_path = tmp_path / "opscloud_test.log"
    configure_logging(level="INFO", log_dir=tmp_path, log_file=test_log_path, log_to_file=True)
    try:
        mock_client = MagicMock()
        mock_client.threads.create = AsyncMock(return_value={"thread_id": "t-log-1"})
        mock_client.runs.create = AsyncMock(return_value={"run_id": "r-log-1"})
        mock_client.runs.join = AsyncMock(return_value=None)
        mock_client.runs.get = AsyncMock(return_value={"status": "success", "error": None})
        mock_client.threads.get_state = AsyncMock(
            return_value={
                "values": {
                    "messages": [
                        {"id": "m1", "role": "user", "content": "Investigate incident"},
                        {
                            "id": "m2",
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {"id": "tc-k8s-1", "name": "execute", "args": {"command": "kubectl get pods -n payments"}}
                            ],
                        },
                        {
                            "id": "m3",
                            "role": "tool",
                            "tool_call_id": "tc-k8s-1",
                            "name": "execute",
                            "status": "success",
                            "content": "payments-api-7f8b9c   0/1   CrashLoopBackOff",
                        },
                        {
                            "id": "m4",
                            "role": "assistant",
                            "content": "## Root Cause Analysis\npayments-api pod OOMKilled due to 256Mi memory limit.",
                        },
                    ]
                }
            }
        )

        dispatcher = WebhookDispatcher(
            config=WebhookConfig(),
            run_executor=default_langgraph_run_executor,
        )

        pd_payload = {
            "event": {
                "id": "01LOGTEST",
                "event_type": "incident.triggered",
                "data": {
                    "id": "Q_LOG_INCIDENT_1",
                    "title": "High Restart Count on payments-api",
                    "urgency": "high",
                    "status": "triggered",
                    "service": {"summary": "payments-service"},
                },
            }
        }

        with patch("langgraph_sdk.get_client", return_value=mock_client):
            status_code, resp = await dispatcher.handle_webhook(
                provider="pagerduty",
                headers={"content-type": "application/json"},
                raw_body=json.dumps(pd_payload).encode("utf-8"),
            )
            assert status_code == 202
            assert resp["status"] == "accepted"
            await dispatcher.drain_background_tasks()

        log_contents = test_log_path.read_text(encoding="utf-8")
        assert "Webhook received | provider=pagerduty event_type=incident.triggered incident_id=Q_LOG_INCIDENT_1" in log_contents
        assert "Webhook accepted & queued |" in log_contents
        assert "Executing background webhook investigation |" in log_contents
        assert "Starting webhook agent run |" in log_contents
        assert "LangGraph run created | run_id=r-log-1" in log_contents
        assert "Webhook agent tool call | run_id=r-log-1" in log_contents
        assert "tool=execute" in log_contents
        assert "Webhook agent tool completed | run_id=r-log-1" in log_contents
        assert "Webhook agent run completed | run_id=r-log-1" in log_contents
        assert "Webhook RCA summary | run_id=r-log-1 incident_id=Q_LOG_INCIDENT_1" in log_contents
        assert "Webhook run finalized | provider=pagerduty incident_id=Q_LOG_INCIDENT_1" in log_contents
    finally:
        configure_logging()
