"""Unit tests for subagent metadata and prompt block generation."""

from opscloud.middleware.subagents import SubagentsMiddleware


def test_subagents_middleware():
    metas = [
        {"name": "eks-specialist", "description": "Diagnoses EKS cluster and node issues"},
        {"name": "cost-optimizer", "description": "Analyzes AWS spend and right-sizing"},
    ]
    mw = SubagentsMiddleware(subagent_metas=metas)
    assert mw.subagent_names == ["cost-optimizer", "eks-specialist"]

    prompt_block = mw._build_prompt_block()
    assert "## Subagent Delegation & Orchestration Architecture" in prompt_block
    assert "### 1. Built-in Subagents (Direct `task` Tool)" in prompt_block
    assert "`eks-specialist`" in prompt_block
    assert "`cost-optimizer`" in prompt_block
    assert "### Subagent Deliverable Presentation Protocol" in prompt_block
    assert "Retain all structured tables" in prompt_block

    # Test dynamic registration of plugin subagent
    mw.register_subagent({
        "name": "plugin-secops",
        "description": "Security compliance scanner",
        "source": "plugin:cloud-security",
        "skills": ["cve-scan"],
    })
    assert "plugin-secops" in mw.subagent_names
    prompt_block_with_plugin = mw._build_prompt_block()
    assert "### 2. Plugin & Extension Subagents (Code Interpreter `js_eval`)" in prompt_block_with_plugin
    assert "`plugin-secops`" in prompt_block_with_plugin
    assert "`cve-scan`" in prompt_block_with_plugin

    # Test before_agent state injection
    state_update = mw.before_agent({}, None)
    assert state_update is not None
    assert "_subagent_registry" in state_update
    assert "eks-specialist" in state_update["_subagent_registry"]
    assert state_update["_subagent_registry"]["plugin-secops"]["is_plugin"] is True

    # Test planning_mode
    mw_planning = SubagentsMiddleware(subagent_metas=metas, planning_mode=True)
    prompt_block_planning = mw_planning._build_prompt_block()
    assert "## Subagent Delegation & Operational Capabilities" in prompt_block_planning
    assert "`eks-specialist`" in prompt_block_planning


def test_get_built_in_subagents_empty():
    """Ensure OpsCloud has no built-in subagents on disk."""
    from opscloud.subagents import get_built_in_subagents

    assert get_built_in_subagents() == []


def test_parse_subagent_file(tmp_path):
    """Test parsing subagent markdown with YAML frontmatter."""
    from opscloud.subagents.loader import parse_subagent_file

    # Valid subagent file
    agent_file = tmp_path / "AGENTS.md"
    agent_file.write_text(
        """---
name: eks-operator
description: Kubernetes and EKS cluster management specialist
model: anthropic:claude-3-5-sonnet-20241022
skills: k8s-diag, helm-ops
tools: kubectl, helm, read_file
permission_tier: tier2
capabilities:
  - cluster_admin
  - log_stream
mcp_config:
  k8s-mcp:
    command: k8s-mcp-server
---

# EKS Operator

You are an expert Kubernetes cluster administrator.
Analyze pod status and diagnose node health.
""",
        encoding="utf-8",
    )

    parsed = parse_subagent_file(agent_file)
    assert parsed is not None
    assert parsed["name"] == "eks-operator"
    assert parsed["description"] == "Kubernetes and EKS cluster management specialist"
    assert parsed["model"] == "anthropic:claude-3-5-sonnet-20241022"
    assert parsed["skills"] == ["k8s-diag", "helm-ops"]
    assert parsed["tools"] == ["kubectl", "helm", "read_file"]
    assert parsed["permission_tier"] == "tier2"
    assert parsed["capabilities"] == ["cluster_admin", "log_stream"]
    assert parsed["mcp_config"] == {"k8s-mcp": {"command": "k8s-mcp-server"}}
    assert "You are an expert Kubernetes cluster administrator." in parsed["system_prompt"]
    assert parsed["source"] == "custom"
    assert parsed["path"] == str(agent_file)


def test_parse_subagent_file_fallback_and_invalid(tmp_path):
    """Test edge cases: missing frontmatter, fallback name, missing description."""
    from opscloud.subagents.loader import parse_subagent_file

    # Missing frontmatter
    no_fm = tmp_path / "plain.md"
    no_fm.write_text("# Just Markdown", encoding="utf-8")
    assert parse_subagent_file(no_fm) is None

    # Missing description
    no_desc = tmp_path / "no_desc.md"
    no_desc.write_text("---\nname: my-agent\n---\nPrompt", encoding="utf-8")
    assert parse_subagent_file(no_desc) is None

    # Fallback name engagement
    fallback_file = tmp_path / "fallback.md"
    fallback_file.write_text("---\ndescription: Fallback test\n---\nPrompt", encoding="utf-8")
    parsed = parse_subagent_file(fallback_file, fallback_name="inferred-agent")
    assert parsed is not None
    assert parsed["name"] == "inferred-agent"
    assert parsed["description"] == "Fallback test"


def test_list_subagents_precedence(tmp_path, monkeypatch):
    """Test user vs project directory precedence and plugin discovery."""
    from opscloud.subagents.loader import list_subagents

    user_dir = tmp_path / "user_agents"
    user_dir.mkdir()
    proj_dir = tmp_path / "proj_agents"
    proj_dir.mkdir()

    # User agent: eks-specialist (will be overridden) and cost-optimizer (will remain)
    (user_dir / "eks-specialist").mkdir()
    (user_dir / "eks-specialist" / "AGENTS.md").write_text(
        "---\ndescription: User EKS agent\n---\nUser prompt", encoding="utf-8"
    )
    (user_dir / "cost-optimizer").mkdir()
    (user_dir / "cost-optimizer" / "AGENTS.md").write_text(
        "---\ndescription: User cost optimizer\n---\nUser cost prompt", encoding="utf-8"
    )

    # Project agent overrides eks-specialist
    (proj_dir / "eks-specialist").mkdir()
    (proj_dir / "eks-specialist" / "AGENTS.md").write_text(
        "---\ndescription: Project EKS agent override\n---\nProject prompt", encoding="utf-8"
    )

    # Mock plugin discovery
    mock_plugin_agent = {
        "name": "terraform-plugin:tf-planner",
        "description": "Terraform plan validator",
        "system_prompt": "Run tf plan",
        "source": "plugin",
        "path": "/fake/path/AGENTS.md",
    }
    monkeypatch.setattr(
        "opscloud.plugins.adapters.agents.discover_plugin_subagents",
        lambda project_root=None: [mock_plugin_agent],
    )

    results = list_subagents(
        user_agents_dir=user_dir,
        project_agents_dir=proj_dir,
        include_plugins=True,
    )

    result_by_name = {r["name"]: r for r in results}
    assert "eks-specialist" in result_by_name
    # Project should override user
    assert result_by_name["eks-specialist"]["description"] == "Project EKS agent override"
    assert result_by_name["eks-specialist"]["source"] == "project"

    assert "cost-optimizer" in result_by_name
    assert result_by_name["cost-optimizer"]["description"] == "User cost optimizer"
    assert result_by_name["cost-optimizer"]["source"] == "user"

    assert "terraform-plugin:tf-planner" in result_by_name
    assert result_by_name["terraform-plugin:tf-planner"]["source"] == "plugin"


def test_load_async_subagents(tmp_path, monkeypatch):
    """Test loading remote async subagents from config.toml with env expansion."""
    from opscloud.subagents.loader import load_async_subagents

    monkeypatch.setenv("OPSCLOUD_REMOTE_HOST", "cloud.internal")
    monkeypatch.setenv("OPSCLOUD_AUTH_TOKEN", "bearer-xyz123")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[async_subagents.remote_secops]
description = "Remote security compliance scanner"
graph_id = "secops-scanner"
url = "https://${OPSCLOUD_REMOTE_HOST}/api/v1"

[async_subagents.remote_secops.headers]
Authorization = "Bearer ${OPSCLOUD_AUTH_TOKEN}"
""",
        encoding="utf-8",
    )

    agents = load_async_subagents(config_file)
    assert len(agents) == 1
    secops = agents[0]
    assert secops["name"] == "remote_secops"
    assert secops["description"] == "Remote security compliance scanner"
    assert secops["graph_id"] == "secops-scanner"
    assert secops["url"] == "https://cloud.internal/api/v1"
    assert secops["headers"] == {"Authorization": "Bearer bearer-xyz123"}


def test_subagent_panel_header_meta_parts():
    """Verify SubagentPanel header rendering and separator glyph resolution."""
    from opscloud.ui.widgets.subagent_panel import SubagentPanel, _Phase
    from opscloud.ui.theme import DARK_COLORS

    panel = SubagentPanel()
    panel._phase_order = ["eval-1", "eval-2"]
    panel._phases["eval-1"] = _Phase(index=1, eval_id="eval-1")
    panel._phases["eval-2"] = _Phase(index=2, eval_id="eval-2")

    parts = panel._header_meta_parts(done=1, total=2, failed=1, cancelled=0, colors=DARK_COLORS)
    assert len(parts) >= 1
    combined = "".join(p.plain for p in parts)
    assert "1/2 done" in combined
    assert "failed" in combined
    assert "·" in combined or "|" in combined

    # Test phase row formatting
    row = panel._phase_row(panel._phases["eval-1"], selected=True, colors=DARK_COLORS)
    assert "eval-1" not in row.plain  # Shows index
    assert "0/0" in row.plain


def test_subagent_panel_model_and_activity():
    """Verify SubagentPanel captures model name and live activity/action."""
    from opscloud.ui.widgets.subagent_panel import SubagentPanel
    from opscloud.ui.theme import DARK_COLORS

    panel = SubagentPanel()
    panel.prepare_turn(model_label="gemini-2.5-flash")

    # Start event with explicit model
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "sub-1",
        "subagent_type": "aws-finops-agent",
        "description": "Assess AWS spend",
        "model": "anthropic:claude-3-5-sonnet",
    })

    phase = panel._displayed_phase()
    assert phase is not None
    record = phase.records["sub-1"]
    assert record.model == "claude-3-5-sonnet"
    assert record.status == "running"

    # Set subagent live activity
    panel.set_subagent_activity("sub-1", "aws_ce_get_cost_and_usage")
    assert record.current_action == "aws_ce_get_cost_and_usage"

    # Verify rendering contains activity and model
    from opscloud.config.settings import get_glyphs
    glyphs = get_glyphs()
    row = panel._render_row(record, 100, glyphs, DARK_COLORS)
    plain = row.plain
    assert "aws_ce_get_cost_and_usage" in plain
    assert "claude-3-5" in plain

    # Finish subagent clears activity
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "complete",
        "id": "sub-1",
    })
    assert record.status == "done"
    assert record.current_action is None


def test_subagent_cli_middleware_hitl():
    """Verify _subagent_cli_middleware attaches AutoModeHITLMiddleware when appropriate."""
    from opscloud.agent.factory import _subagent_cli_middleware
    from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware, AsyncApprovalHITLMiddleware

    interrupt_on = {"execute": True}

    # Interactive + not auto_approve -> AutoModeHITLMiddleware
    mw_list = _subagent_cli_middleware(
        has_explicit_model=True,
        assistant_id="asst-1",
        subagent_name="test-agent",
        interactive=True,
        auto_approve=False,
        shell_allow_list=["ls", "cat"],
        interrupt_on=interrupt_on,
    )
    assert any(isinstance(m, AutoModeHITLMiddleware) for m in mw_list)
    assert not any(type(m) is AsyncApprovalHITLMiddleware for m in mw_list)

    # auto_approve=True -> AsyncApprovalHITLMiddleware
    mw_list_auto = _subagent_cli_middleware(
        has_explicit_model=True,
        assistant_id="asst-1",
        subagent_name="test-agent",
        interactive=True,
        auto_approve=True,
        shell_allow_list=["ls", "cat"],
        interrupt_on=interrupt_on,
    )
    assert any(type(m) is AsyncApprovalHITLMiddleware for m in mw_list_auto)
    assert not any(isinstance(m, AutoModeHITLMiddleware) for m in mw_list_auto)


def test_subagent_cost_transfers_empty_scope():
    """Verify CostTrackingMiddleware transfers costs from nested subagents without checkpoint_ns."""
    from unittest.mock import MagicMock
    from opscloud.middleware.cost_tracking import CostTrackingMiddleware
    from langgraph.types import Overwrite

    nested_mw = CostTrackingMiddleware(nested=True)
    root_mw = CostTrackingMiddleware(nested=False)

    # Simulate nested subagent state with some cost and tokens
    subagent_state = {
        "_session_cost_usd": 0.15,
        "_session_total_tokens": 12000,
    }
    mock_runtime = MagicMock()
    mock_runtime.execution_info = None
    mock_runtime.context = {"thread_id": "thread-1"}

    # Run _after_agent_update for nested subagent
    update = nested_mw._after_agent_update(subagent_state, mock_runtime)
    assert update is not None
    assert "_session_cost_transfers" in update
    transfers = update["_session_cost_transfers"]
    if isinstance(transfers, Overwrite):
        transfers = transfers.value
    assert len(transfers) >= 1
    # Check that owner_scope is empty string so root can claim it
    entry = next(iter(transfers.values()))
    assert entry["cost_usd"] == 0.15
    assert entry["tokens"] == 12000
    assert entry["owner_scope"] == ""

    # Now verify root charges and claims the transfer
    root_state = {
        "_session_cost_transfers": transfers,
        "_session_cost_usd": 0.05,
        "_session_total_tokens": 500,
        "messages": [],
    }
    root_runtime = MagicMock()
    root_runtime.execution_info = None
    root_runtime.context = {"thread_id": "thread-1"}
    root_update = root_mw._charge(root_state, root_runtime, price_latest_message=False)
    assert root_update is not None
    assert root_update.get("_session_cost_usd") == 0.15
    assert root_update.get("_session_total_tokens") == 12000


def test_subagent_telemetry_format_tool_activity():
    from opscloud.middleware.subagent_telemetry import _format_tool_activity

    assert _format_tool_activity("bash", {"command": "aws eks list-clusters --region us-east-1"}) == "aws eks list-clusters --region us-east-1"
    assert _format_tool_activity("run_command", {"command": "kubectl get pods -n kube-system"}) == "kubectl get pods -n kube-system"
    assert _format_tool_activity("read_file", {"file_path": "/var/log/syslog"}) == "read_file: /var/log/syslog"
    assert _format_tool_activity("edit_file", {"path": "src/main.py"}) == "edit_file: src/main.py"
    assert _format_tool_activity("custom_tool", {"foo": "bar"}) == "custom_tool"


def test_subagent_telemetry_middleware_wrap_tool_call():
    from unittest.mock import MagicMock
    from opscloud.middleware.subagent_telemetry import SubagentTelemetryMiddleware

    mw = SubagentTelemetryMiddleware(subagent_name="aws-finops-agent")

    written_events = []
    runtime = MagicMock()
    runtime.stream_writer = lambda e: written_events.append(e)
    runtime.config = {}

    request = MagicMock()
    request.runtime = runtime
    request.tool_call = {
        "name": "bash",
        "args": {"command": "aws ec2 describe-instances"},
    }

    called = False

    def dummy_handler(req):
        nonlocal called
        called = True
        return "tool_result"

    res = mw.wrap_tool_call(request, dummy_handler)
    assert called is True
    assert res == "tool_result"
    assert len(written_events) == 2
    event_start = written_events[0]
    assert event_start["type"] == "subagent_progress"
    assert event_start["subagent_name"] == "aws-finops-agent"
    assert "aws ec2 describe-instances" in event_start["activity"]
    assert event_start["tool_name"] == "bash"

    event_end = written_events[1]
    assert event_end["type"] == "subagent_progress"
    assert event_end["activity"] == "Thinking..."


def test_subagent_telemetry_middleware_wrap_model_call():
    from unittest.mock import MagicMock
    from opscloud.middleware.subagent_telemetry import SubagentTelemetryMiddleware

    mw = SubagentTelemetryMiddleware(subagent_name="aws-finops-agent")

    written_events = []
    runtime = MagicMock()
    runtime.stream_writer = lambda e: written_events.append(e)
    runtime.config = {}

    request = MagicMock()
    request.runtime = runtime

    called = False

    def dummy_handler(req):
        nonlocal called
        called = True
        return "model_response"

    res = mw.wrap_model_call(request, dummy_handler)
    assert called is True
    assert res == "model_response"
    assert len(written_events) == 1
    event = written_events[0]
    assert event["type"] == "subagent_progress"
    assert event["subagent_name"] == "aws-finops-agent"
    assert event["activity"] == "Thinking..."
    assert event["tool_name"] is None


def test_subagent_transcript_config_thread_id_binding():
    from unittest.mock import MagicMock
    from opscloud.middleware.server_hooks import _subagent_transcript_config
    from opscloud.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID
    from langchain_core.runnables.config import var_child_runnable_config

    call = MagicMock()
    call.name = "task"
    call.id = "call_abc123"

    config = {
        "configurable": {"thread_id": "thread-main-777"},
        "metadata": {"some_key": "val"},
    }

    assert ACTIVE_SESSION_THREAD_ID.get() is None

    with _subagent_transcript_config(call, config):
        child_cfg = var_child_runnable_config.get()
        assert child_cfg is not None
        assert child_cfg["metadata"]["subagent_transcript_id"] == "call_abc123"
        assert child_cfg["metadata"]["thread_id"] == "thread-main-777"
        assert child_cfg["metadata"]["checkpoint_ns"] == "subagent:call_abc123"
        assert child_cfg["configurable"]["thread_id"] == "thread-main-777"
        assert child_cfg["configurable"]["checkpoint_ns"] == "subagent:call_abc123"
        assert ACTIVE_SESSION_THREAD_ID.get() == "thread-main-777"

    assert ACTIVE_SESSION_THREAD_ID.get() is None


def test_subagent_panel_activity_matching_by_name():
    from opscloud.ui.widgets.subagent_panel import SubagentPanel

    panel = SubagentPanel()
    # Add a subagent run
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "tool_call_xyz",
        "subagent_type": "aws-finops-agent",
        "description": "Analyze AWS costs",
    })

    # Update activity using subagent name instead of call id
    panel.set_subagent_activity("aws-finops-agent", "Running aws ec2 describe-instances")
    rec = panel._find_record("tool_call_xyz")
    assert rec is not None
    assert rec.current_action == "Running aws ec2 describe-instances"


def test_subagent_transcript_config_js_eval():
    """Verify _subagent_transcript_config also binds thread and config for js_eval."""
    from unittest.mock import MagicMock
    from opscloud.middleware.server_hooks import _subagent_transcript_config
    from opscloud.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID
    from langchain_core.runnables.config import var_child_runnable_config

    call = MagicMock()
    call.name = "js_eval"
    call.id = "eval_call_456"

    config = {
        "configurable": {"thread_id": "thread-eval-999"},
        "metadata": {"some_key": "val"},
    }

    assert ACTIVE_SESSION_THREAD_ID.get() is None

    with _subagent_transcript_config(call, config):
        child_cfg = var_child_runnable_config.get()
        assert child_cfg is not None
        assert child_cfg["metadata"]["subagent_transcript_id"] == "eval_call_456"
        assert child_cfg["metadata"]["thread_id"] == "thread-eval-999"
        assert child_cfg["configurable"]["thread_id"] == "thread-eval-999"
        assert ACTIVE_SESSION_THREAD_ID.get() == "thread-eval-999"

    assert ACTIVE_SESSION_THREAD_ID.get() is None


def test_subagent_cli_middleware_includes_jev_router():
    """Verify _subagent_cli_middleware includes JevDynamicModelRouterMiddleware."""
    from opscloud.agent.factory import _subagent_cli_middleware
    from opscloud.middleware.jev_model_router import JevDynamicModelRouterMiddleware

    mw_list = _subagent_cli_middleware(
        has_explicit_model=False,
        assistant_id="asst-1",
        subagent_name="aws-finops-agent",
        interactive=True,
        auto_approve=False,
    )
    assert any(isinstance(m, JevDynamicModelRouterMiddleware) for m in mw_list)


def test_subagent_cli_middleware_includes_compaction_middleware():
    """Verify _subagent_cli_middleware attaches CLICompactionMiddleware when backend and model are provided."""
    from unittest.mock import MagicMock
    from langchain_core.language_models.chat_models import BaseChatModel
    from deepagents.backends.filesystem import FilesystemBackend
    from opscloud.agent.factory import _subagent_cli_middleware
    from opscloud.middleware.compaction import CLICompactionMiddleware
    from opscloud.middleware.jev_model_router import JevDynamicModelRouterMiddleware

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.profile = {"max_input_tokens": 128_000}
    backend = FilesystemBackend()

    mw_list = _subagent_cli_middleware(
        has_explicit_model=False,
        assistant_id="asst-1",
        subagent_name="aws-finops-agent",
        interactive=True,
        auto_approve=False,
        backend=backend,
        model=mock_model,
    )

    compaction_mw = next((m for m in mw_list if isinstance(m, CLICompactionMiddleware)), None)
    assert compaction_mw is not None, "CLICompactionMiddleware should be attached to subagent"

    # Verify pipeline order: Jev router comes before compaction
    jev_idx = next(i for i, m in enumerate(mw_list) if isinstance(m, JevDynamicModelRouterMiddleware))
    compaction_idx = next(i for i, m in enumerate(mw_list) if isinstance(m, CLICompactionMiddleware))
    assert jev_idx < compaction_idx, "JevDynamicModelRouterMiddleware must precede CLICompactionMiddleware"


def test_subagent_tool_filter_allows_system_compaction_tool():
    """Verify ToolFilterMiddleware does not block compact_conversation even with strict allowed_tools."""
    from opscloud.middleware.tool_filter import ToolFilterMiddleware, ALWAYS_ALLOWED_SYSTEM_TOOLS

    assert "compact_conversation" in ALWAYS_ALLOWED_SYSTEM_TOOLS
    assert "ask_user" in ALWAYS_ALLOWED_SYSTEM_TOOLS

    tf = ToolFilterMiddleware(
        allowed_patterns=["read_file", "write_file"],
        subagent_name="strict-subagent",
    )
    assert tf.is_tool_allowed("compact_conversation") is True
    assert tf.is_tool_allowed("ask_user") is True
    assert tf.is_tool_allowed("read_file") is True
    assert tf.is_tool_allowed("execute") is False



def test_pending_thread_transfers_bridge_to_charge():
    """Verify _PENDING_THREAD_TRANSFERS bridges dropped subagent transfers to parent _charge."""
    from unittest.mock import MagicMock
    from opscloud.middleware.cost_tracking import (
        CostTrackingMiddleware,
        _PENDING_THREAD_TRANSFERS,
        _TRANSFERS_LOCK,
        _CostTransfer,
    )

    thread_id = "test-thread-bridge-1"
    sub_scope = "subagent:test-thread-bridge-1:sub1"
    transfer_entry: _CostTransfer = {
        "owner_scope": "",
        "cost_usd": 0.1993,
        "tokens": 835000,
    }

    with _TRANSFERS_LOCK:
        _PENDING_THREAD_TRANSFERS.setdefault(thread_id, {})[sub_scope] = transfer_entry

    mw_parent = CostTrackingMiddleware(nested=False)
    runtime = MagicMock()
    runtime.execution_info.thread_id = thread_id
    runtime.execution_info.checkpoint_ns = ""
    runtime.context = {"thread_id": thread_id}
    runtime.config = {"configurable": {"thread_id": thread_id}}
    runtime.stream_writer = MagicMock()

    # State with NO _session_cost_transfers (simulating dropped state from js_eval)
    state: dict[str, Any] = {
        "messages": [],
        "_session_cost_usd": 0.05,
        "_session_total_tokens": 10000,
    }

    update = mw_parent._charge(state, runtime, price_latest_message=False)
    assert update is not None
    assert update.get("_session_cost_usd") == 0.1993
    assert update.get("_session_total_tokens") == 835000

    # Ensure pending transfers was drained and not duplicated
    with _TRANSFERS_LOCK:
        assert thread_id not in _PENDING_THREAD_TRANSFERS


def test_tool_group_summary_js_eval_categorization():
    """Verify ToolGroupSummary formats js_eval as 'Ran 1 JS evaluation'."""
    from opscloud.ui.widgets.messages import summarize_tool_group

    res = summarize_tool_group(["js_eval"], tense="past")
    assert res == "Ran 1 JS evaluation"

    res_live = summarize_tool_group(["js_eval"], tense="present")
    assert res_live == "Running 1 JS evaluation"


def test_subagent_panel_set_subagent_model():
    """Verify SubagentPanel.set_subagent_model dynamically updates model label."""
    from opscloud.ui.widgets.subagent_panel import SubagentPanel

    panel = SubagentPanel()
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "sub_xyz",
        "subagent_type": "aws-finops-agent",
        "description": "Analyze AWS costs",
    })
    rec = panel._find_record("sub_xyz")
    assert rec is not None

    panel.set_subagent_model("sub_xyz", "claude-3-7-sonnet")
    assert rec.model == "claude-3-7-sonnet"


def test_subagent_panel_hitl_resume_deduplication():
    """Verify that when a subagent is interrupted for HITL approval and resumed with a new UUID,
    SubagentPanel does not create duplicate rows or inflate phase counts."""
    from opscloud.ui.widgets.subagent_panel import SubagentPanel
    from opscloud.ui.theme import DARK_COLORS
    from opscloud.config.settings import get_glyphs

    panel = SubagentPanel()
    eval_id = "eval_call_101"

    # 1. Initial start event
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "ptc_task_uuid_1",
        "eval_id": eval_id,
        "subagent_type": "aws-finops-agent@talkops-devops-plugins",
        "description": "Perform a comprehensive AWS FinOps cost optimization audit for all regions",
        "label": "Perform a comprehensive AWS FinOps cost optimization audit f",
        "model": "gemini-3.7-flash",
    })

    phase = panel._phases[eval_id]
    assert phase.counts() == (0, 1)
    assert len(phase.order) == 1
    assert phase.order == ["ptc_task_uuid_1"]
    rec1 = phase.records["ptc_task_uuid_1"]
    assert rec1.subagent_type == "aws-finops-agent"
    assert "Perform a comprehensive AWS FinOps" in rec1.label
    assert not rec1.label.endswith("audit f")  # Clean word boundary, not chopped at 60 chars
    start_time = rec1.started_monotonic

    # 2. Activity before interrupt
    panel.set_subagent_activity("ptc_task_uuid_1", "aws: running script")
    assert rec1.current_action == "aws: running script"

    # 3. Simulate first approval resume (QuickJS re-invokes task with new random UUID)
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "ptc_task_uuid_2",
        "eval_id": eval_id,
        "subagent_type": "aws-finops-agent@talkops-devops-plugins",
        "description": "Perform a comprehensive AWS FinOps cost optimization audit for all regions",
        "label": "Perform a comprehensive AWS FinOps cost optimization audit f",
        "model": "gemini-3.7-flash",
    })

    # Counts must NOT inflate to 0/2; order must NOT have 2 items
    assert phase.counts() == (0, 1)
    assert len(phase.order) == 1
    assert phase.order == ["ptc_task_uuid_2"]
    rec2 = phase.records["ptc_task_uuid_2"]
    assert rec2 is rec1  # Same record reused!
    assert rec2.started_monotonic == start_time  # True start time preserved!

    # 4. Simulate second approval resume
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "start",
        "id": "ptc_task_uuid_3",
        "eval_id": eval_id,
        "subagent_type": "aws-finops-agent@talkops-devops-plugins",
        "description": "Perform a comprehensive AWS FinOps cost optimization audit for all regions",
        "label": "Perform a comprehensive AWS FinOps cost optimization audit f",
        "model": "gemini-3.7-flash",
    })

    assert phase.counts() == (0, 1)
    assert len(phase.order) == 1
    assert phase.order == ["ptc_task_uuid_3"]

    # Render row check
    glyphs = get_glyphs()
    row = panel._render_row(rec2, 120, glyphs, DARK_COLORS)
    plain = row.plain
    assert "aws-finops-agent" in plain
    assert "@talkops-devops-plugins" not in plain  # Clean plugin prefix
    assert "gemini-3.7-flash" in plain

    # 5. Complete subagent with latest UUID
    panel.on_subagent_event({
        "type": "subagent",
        "phase": "complete",
        "id": "ptc_task_uuid_3",
        "eval_id": eval_id,
        "duration_ms": 125000,
    })

    assert phase.counts() == (1, 1)  # Accurately 1/1 done!
    assert rec2.status == "done"
    assert rec2.current_action is None


def test_subagent_telemetry_mcp_run_script_formatting():
    """Verify _format_tool_activity extracts meaningful context from MCP script executions."""
    from opscloud.middleware.subagent_telemetry import _format_tool_activity

    # Script with comment
    code1 = "# Query AWS Cost Explorer for unblended costs\nimport boto3\nce = boto3.client('ce')\n"
    res1 = _format_tool_activity("aws___run_script", {"script": code1})
    assert res1 == "aws: Query AWS Cost Explorer for unblended costs"

    # Script with boto3 client call
    code2 = "import boto3\nec2 = boto3.client('ec2')\nec2.describe_volumes()\n"
    res2 = _format_tool_activity("aws___run_script", {"script": code2})
    assert res2 == "aws: ec2 query"

    # Tool with explicit description
    res3 = _format_tool_activity("plugin__aws-compute__aws___run_script", {
        "description": "Check unattached EBS volumes",
    })
    assert res3 == "aws: Check unattached EBS volumes"

    # Tool with command
    res4 = _format_tool_activity("aws___run_script", {
        "command": "aws ce get-cost-and-usage --time-period Start=2026-05-01,End=2026-10-01",
    })
    assert "aws ce get-cost-and-usage" in res4
    assert not res4.startswith("aws: aws")






