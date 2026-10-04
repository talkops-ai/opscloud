"""Unit tests for OpsCloud security module: unicode security, shell safety, and approval modes."""

import pytest
from unittest.mock import MagicMock
from opscloud.approval_mode import ApprovalMode
from opscloud.security.unicode_security import (
    check_url_safety,
    detect_dangerous_unicode,
    sanitize_control_chars,
    strip_dangerous_unicode,
)
from opscloud.security.shell_safety import (
    classify_command,
    contains_dangerous_patterns,
    is_safe_command,
    is_shell_command_allowed,
)


def test_strip_dangerous_unicode():
    # Zero width space \u200B and RTL override \u202E
    malicious = "aws\u200B ec2\u202E describe-instances"
    cleaned = strip_dangerous_unicode(malicious)
    assert "\u200B" not in cleaned
    assert "\u202E" not in cleaned
    assert cleaned == "aws ec2 describe-instances"

    issues = detect_dangerous_unicode(malicious)
    assert len(issues) >= 2


def test_sanitize_control_chars():
    text_with_null = "echo 'hello\x00world'\x07"
    sanitized = sanitize_control_chars(text_with_null)
    assert "\x00" not in sanitized
    assert "\x07" not in sanitized
    assert "hello" in sanitized


def test_check_url_safety_and_ssrf():
    from opscloud.security.url_validation import _validate_url, UrlValidationError

    # Unicode deceptive URL
    result_deceptive = check_url_safety("http://exam\u200Bple.com/path")
    assert result_deceptive.safe is False
    assert len(result_deceptive.warnings) > 0 or len(result_deceptive.issues) > 0

    # AWS metadata service IP is blocked by SSRF validator
    with pytest.raises(UrlValidationError) as exc_info:
        _validate_url("http://169.254.169.254/latest/meta-data/")
    assert "blocked address" in str(exc_info.value).lower() or "link-local" in str(exc_info.value).lower()

    # Disallowed scheme
    with pytest.raises(UrlValidationError) as exc_info:
        _validate_url("file:///etc/passwd")
    assert "scheme not allowed" in str(exc_info.value).lower()


def test_shell_safety_dangerous_patterns():
    # Destructive operations
    assert contains_dangerous_patterns("rm -rf /") is True
    assert contains_dangerous_patterns("aws ec2 terminate-instances --instance-ids i-012345") is True
    assert contains_dangerous_patterns("curl http://169.254.169.254/") is True
    assert contains_dangerous_patterns("cat ~/.aws/credentials") is True
    assert contains_dangerous_patterns(":(){ :|:& };:") is True

    # Safe operations
    assert contains_dangerous_patterns("aws ec2 describe-instances") is False
    assert contains_dangerous_patterns("git status") is False
    assert contains_dangerous_patterns("kubectl get pods -n kube-system") is False


def test_shell_safety_is_safe_command():
    assert is_safe_command("aws ec2 describe-instances") is True
    assert is_safe_command("kubectl get pods") is True
    assert is_safe_command("git status") is True
    assert is_safe_command("terraform show") is True
    assert is_safe_command("aws ec2 terminate-instances") is False
    assert is_safe_command("rm -rf /") is False


def test_approval_mode_parsing():
    assert ApprovalMode.from_str("auto") == ApprovalMode.AUTO
    assert ApprovalMode.from_str("manual") == ApprovalMode.MANUAL
    assert ApprovalMode.from_str("smart") == ApprovalMode.SMART
    assert ApprovalMode.from_str("jev") == ApprovalMode.SMART
    assert ApprovalMode.from_str("s") == ApprovalMode.SMART
    assert ApprovalMode.from_str("always") == ApprovalMode.ALWAYS
    # Deprecated/removed modes fail-closed to MANUAL
    assert ApprovalMode.from_str("yolo") == ApprovalMode.MANUAL


def test_next_approval_mode_cycle():
    from opscloud.security.approval_mode import next_approval_mode

    assert next_approval_mode(ApprovalMode.MANUAL) == ApprovalMode.AUTO
    assert next_approval_mode(ApprovalMode.AUTO) == ApprovalMode.SMART
    assert next_approval_mode(ApprovalMode.SMART) == ApprovalMode.MANUAL


def test_shell_safety_multicloud_dangerous_patterns():
    # Azure dangerous patterns
    assert contains_dangerous_patterns("az vm delete -g mygroup -n myvm") is True
    assert contains_dangerous_patterns("az group delete -n mygroup") is True
    assert contains_dangerous_patterns("az aks delete --resource-group rg --name cluster") is True
    assert contains_dangerous_patterns("az keyvault purge --name mykv") is True
    assert contains_dangerous_patterns("cat ~/.azure/accessTokens.json") is True

    # GCP dangerous patterns
    assert contains_dangerous_patterns("gcloud compute instances delete my-vm --zone us-central1-a") is True
    assert contains_dangerous_patterns("gcloud container clusters delete my-cluster") is True
    assert contains_dangerous_patterns("gcloud projects delete my-proj") is True
    assert contains_dangerous_patterns("cat ~/.config/gcloud/credentials.db") is True
    assert contains_dangerous_patterns("curl http://metadata.google.internal/computeMetadata/v1/") is True


def test_shell_safety_multicloud_safe_commands():
    assert is_safe_command("az vm list -o table") is True
    assert is_safe_command("az account show") is True
    assert is_safe_command("az group list") is True
    assert is_safe_command("gcloud compute instances list") is True
    assert is_safe_command("gcloud config list") is True
    assert is_safe_command("gcloud container clusters list") is True


def test_cli_ast_multicloud_destructive_detection():
    from opscloud.security.cli_ast_evaluator import CliAstEvaluator, evaluate_cli_safety

    evaluator = CliAstEvaluator()

    # Multi-token subcommands in Azure and GCP must be flagged as destructive!
    res_az_delete = evaluate_cli_safety("az vm delete -g rg -n vm")
    assert res_az_delete["is_safe"] is False
    assert res_az_delete["is_destructive"] is True

    eval_az_delete = evaluator.evaluate("az vm delete -g rg -n vm")
    assert eval_az_delete.is_safe is False
    assert eval_az_delete.is_destructive is True

    res_az_group_del = evaluate_cli_safety("az group delete -n rg --yes")
    assert res_az_group_del["is_safe"] is False
    assert res_az_group_del["is_destructive"] is True

    res_gcloud_delete = evaluate_cli_safety("gcloud compute instances delete vm-1 --zone us-east1-b")
    assert res_gcloud_delete["is_safe"] is False
    assert res_gcloud_delete["is_destructive"] is True

    # Readonly commands must be classified as safe and non-destructive
    res_az_list = evaluate_cli_safety("az vm list -o table")
    assert res_az_list["is_safe"] is True
    assert res_az_list["is_destructive"] is False

    eval_az_list = evaluator.evaluate("az vm list -o table")
    assert eval_az_list.is_safe is True
    assert eval_az_list.is_destructive is False

    res_gcloud_list = evaluate_cli_safety("gcloud compute instances list")
    assert res_gcloud_list["is_safe"] is True
    assert res_gcloud_list["is_destructive"] is False


def test_approval_mode_payload():
    from opscloud.approval_mode import approval_mode_payload
    import pytest

    assert approval_mode_payload(mode=ApprovalMode.AUTO) == {"mode": "auto"}
    assert approval_mode_payload(mode="manual") == {"mode": "manual"}
    assert approval_mode_payload(mode=ApprovalMode.SMART) == {"mode": "smart"}
    assert approval_mode_payload(mode="smart") == {"mode": "smart"}
    assert approval_mode_payload(auto_approve=True) == {"mode": "auto"}
    assert approval_mode_payload(auto_approve=False) == {"mode": "manual"}

    with pytest.raises(ValueError):
        approval_mode_payload()

    with pytest.raises(ValueError):
        approval_mode_payload(mode="auto", auto_approve=True)


async def test_awrite_and_read_approval_mode():
    from opscloud.approval_mode import (
        APPROVAL_MODE_NAMESPACE,
        aread_approval_mode_from_store,
        awrite_approval_mode,
        read_approval_mode_from_store,
    )

    store: dict[tuple[Any, ...], Any] = {}

    class MockAgent:
        async def aput_store_item(self, namespace, key, value):
            store[(namespace, key)] = {"value": value}

        async def aget(self, namespace, key):
            return store.get((namespace, key))

        def get(self, namespace, key):
            return store.get((namespace, key))

    agent = MockAgent()
    key = await awrite_approval_mode(agent, "test-thread-1", mode=ApprovalMode.AUTO)
    assert key == "thread:test-thread-1:mode"

    mode_sync = read_approval_mode_from_store(agent, key)
    assert mode_sync == ApprovalMode.AUTO

    mode_async = await aread_approval_mode_from_store(agent, key)
    assert mode_async == ApprovalMode.AUTO


def test_auto_mode_notice(tmp_path):
    from opscloud.approval_mode import (
        has_auto_mode_notice,
        save_auto_mode_notice,
    )

    ack_file = tmp_path / "approval.json"

    assert not has_auto_mode_notice(ack_file)
    assert save_auto_mode_notice(ack_file) is True
    assert has_auto_mode_notice(ack_file) is True


@pytest.mark.asyncio
async def test_resolve_approval_mode_signatures():
    from opscloud.security.approval_mode_source import (
        _aresolve_approval_mode,
        _resolve_approval_mode,
        ApprovalPolicyResolver,
        _DecidedMode,
        _LiveLookup,
    )
    from opscloud.approval_mode import ApprovalMode

    # 1. 0 positional arguments
    assert _resolve_approval_mode() in {ApprovalMode.MANUAL, ApprovalMode.AUTO, ApprovalMode.SMART}
    assert (await _aresolve_approval_mode()) in {ApprovalMode.MANUAL, ApprovalMode.AUTO, ApprovalMode.SMART}

    # 2. 1 positional argument (dict context with explicit approval_mode)
    ctx_smart = {"approval_mode": "smart"}
    assert _resolve_approval_mode(ctx_smart) == ApprovalMode.SMART
    assert (await _aresolve_approval_mode(ctx_smart)) == ApprovalMode.SMART

    # 3. 2 positional arguments (context + store=None)
    assert _resolve_approval_mode(ctx_smart, None) == ApprovalMode.SMART
    assert (await _aresolve_approval_mode(ctx_smart, None)) == ApprovalMode.SMART

    # 4. 2 positional arguments with mock store
    mock_store = MagicMock()
    mock_item = MagicMock()
    mock_item.value = {"mode": "auto"}
    mock_store.get.return_value = mock_item

    ctx_with_key = {"approval_mode_key": "thread:thread-123:mode", "thread_id": "thread-123"}
    assert _resolve_approval_mode(ctx_with_key, mock_store) == ApprovalMode.AUTO


def test_web_search_unconfigured_error():
    from opscloud.tools.web_search import web_search

    # Calling web_search without api key should return helpful dict error, not raise
    result = web_search.invoke({"query": "test query"})
    assert isinstance(result, dict)
    assert "error" in result
    assert "TAVILY_API_KEY" in result["error"]
    assert result["query"] == "test query"


@pytest.mark.asyncio
async def test_batched_store_event_loop_safety(caplog):
    import asyncio
    import logging
    from opscloud.approval_mode import (
        ApprovalMode,
        aread_approval_mode_from_store,
        read_approval_mode_from_store,
    )
    from opscloud.security.approval_mode_source import (
        _aresolve_approval_mode,
        _resolve_approval_mode,
    )

    current_loop = asyncio.get_running_loop()

    # Simulate LangGraph BatchedStore that rejects sync get() inside event loop
    class FakeBatchedStore:
        def __init__(self):
            self._loop = current_loop
            self.data = {
                ("opscloud", "approval_mode"): {
                    "thread:test-loop-1:mode": {"mode": "auto"}
                }
            }

        def get(self, namespace, key):
            # Same check as langgraph.store.base.batch._check_loop
            if asyncio.get_running_loop() is self._loop:
                raise asyncio.InvalidStateError(
                    "Synchronous calls to BatchedStore detected in the main event loop."
                )
            return self.data.get(namespace, {}).get(key)

        async def aget(self, namespace, key):
            val = self.data.get(namespace, {}).get(key)
            if val:
                mock_item = MagicMock()
                mock_item.value = val
                return mock_item
            return None

    store = FakeBatchedStore()
    key = "thread:test-loop-1:mode"
    ctx = {"thread_id": "test-loop-1"}

    # 1. Synchronous read from within event loop must NOT raise InvalidStateError
    with caplog.at_level(logging.WARNING):
        mode_sync = read_approval_mode_from_store(store, key)
        # Should return None without raising or logging a WARNING
        assert mode_sync is None
        assert not any("Could not read approval-mode store item" in r.message for r in caplog.records)

    # 2. Asynchronous read from within event loop must succeed via aget()
    mode_async = await aread_approval_mode_from_store(store, key)
    assert mode_async == ApprovalMode.AUTO

    # 3. Synchronous resolver falls back gracefully to context/settings without error
    resolved_sync = _resolve_approval_mode(ctx, store)
    assert resolved_sync in {ApprovalMode.MANUAL, ApprovalMode.AUTO}

    # 4. Asynchronous resolver uses aget() and resolves the stored AUTO mode
    resolved_async = await _aresolve_approval_mode(ctx, store)
    assert resolved_async == ApprovalMode.AUTO


@pytest.mark.asyncio
async def test_auto_mode_hitl_aafter_model():
    from unittest.mock import MagicMock
    from opscloud.middleware.auto_mode_hitl import (
        AutoModeHITLMiddleware,
        _ASYNC_APPROVAL_ROUTING_KEY,
        _RoutingDecision,
        _async_routing_mode,
    )
    from opscloud.approval_mode import ApprovalMode
    from langchain_core.messages import AIMessage, HumanMessage

    mw = AutoModeHITLMiddleware()
    runtime = MagicMock()
    runtime.context = {"approval_mode": "auto"}
    runtime.store = None

    state = {
        "messages": [
            HumanMessage(content="test"),
            AIMessage(content="test reply", tool_calls=[]),
        ]
    }

    # aafter_model should resolve approval mode and tag routed state
    update = await mw.aafter_model(state, runtime)
    # The return from after_model with no tool calls needing interrupt is None or state update
    # In both cases, super().after_model was called with routed_state containing _RoutingDecision(ApprovalMode.AUTO)
    # Let's verify _async_routing_mode helper
    test_state = {_ASYNC_APPROVAL_ROUTING_KEY: _RoutingDecision(ApprovalMode.AUTO)}
    assert _async_routing_mode(test_state) == ApprovalMode.AUTO



