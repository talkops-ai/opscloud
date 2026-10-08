"""Comprehensive unit tests for OpsCloud MCP subsystem.

Covers:
- Environment interpolation and alias synchronization (AWS/K8s)
- Argument normalization and schema cleanup
- Cloud & AWS error diagnostic hints
- Local and DB-backed configuration discovery
- SQLite persistence of MCP server configurations
- Raw MCP configuration import and export (.mcp.json / Claude Desktop format)
- MCP server probing, preloading, and caching
- Semantic Profiler 4-tier risk classification for AWS/Cloud tools
- Headless MCP Guard middleware
- MCP Context middleware prompt formatting
- Session manager and cached tool invocation
- CLI mcp command execution
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from opscloud.cli.commands.mcp import run_mcp_command
from opscloud.config.adapters.sqlite import SqliteConfigAdapter
from opscloud.config.store import ConfigStore
from opscloud.mcp.config import (
    _interpolate_env,
    resolve_mcp_server_env,
    sync_mcp_env_aliases,
)
from opscloud.mcp.discovery import MCPDiscovery, discover_mcp_configs
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.mcp.preload import (
    _mcp_tool_name,
    clear_cached_mcp_server_infos,
    get_cached_mcp_server_infos,
    preload_mcp_metadata,
    preload_mcp_server_info,
    probe_one_mcp_server,
)
from opscloud.mcp.raw_config import export_raw_mcp_config, import_raw_mcp_config
from opscloud.mcp.semantic_profiler import MCPSemanticProfiler, ToolSafetyProfile
from opscloud.mcp.session_manager import (
    MCPSessionManager,
    _build_cached_mcp_tool,
    _clean_mcp_schema,
    _enhance_mcp_error_diagnostics,
    _is_transient_session_error,
    _normalize_mcp_arguments,
    create_mcp_connection,
)
from opscloud.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
    mcp_tool_is_coherently_read_only,
)
from opscloud.middleware.mcp_context import (
    MCPContextMiddleware,
    _build_mcp_context_from_infos,
)


# ── Config and Environment Interpolation ──────────────────────────────────────


def test_interpolate_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_AWS_REGION", raising=False)
    # Default fallback
    assert _interpolate_env("${TEST_AWS_REGION:-us-west-2}", field="test.region") == "us-west-2"

    # Set value
    monkeypatch.setenv("TEST_AWS_REGION", "eu-central-1")
    assert _interpolate_env("${TEST_AWS_REGION:-us-west-2}", field="test.region") == "eu-central-1"

    # Unset without default raises RuntimeError
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(RuntimeError):
        _interpolate_env("${MISSING_VAR}", field="test.missing")


def test_resolve_mcp_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE_NAME", "production")
    raw_config = {
        "command": "uvx",
        "args": ["mcp-server-aws", "--profile", "${AWS_PROFILE_NAME:-default}"],
        "env": {"AWS_REGION": "${AWS_REGION:-us-east-1}"},
    }
    resolved = resolve_mcp_server_env("aws-mcp", raw_config)
    assert resolved["args"] == ["mcp-server-aws", "--profile", "production"]
    assert resolved["env"]["AWS_REGION"] == "us-east-1"


def test_sync_mcp_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    # AWS_REGION <-> AWS_DEFAULT_REGION
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_DEFAULT_REGION") == "ap-south-1"

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.delenv("AWS_REGION", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_REGION") == "us-east-2"

    # AWS_PROFILE <-> AWS_DEFAULT_PROFILE
    monkeypatch.setenv("AWS_PROFILE", "stage")
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_DEFAULT_PROFILE") == "stage"


# ── Argument Normalization & Schema Cleanup ───────────────────────────────────


def test_normalize_mcp_arguments() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "tag": {"type": "string"},
        },
        "required": ["query"],
    }
    raw_args = {
        "query": "active instances",
        "tag": "",  # non-required string with empty string should be stripped
        "limit": 10,
    }
    normalized = _normalize_mcp_arguments(raw_args, schema)
    assert "tag" not in normalized
    assert normalized["query"] == "active instances"
    assert normalized["limit"] == 10


def test_clean_mcp_schema() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "instance_id": {
                "type": "string",
                "additionalProperties": False,
                "$schema": "...",
            }
        },
    }
    cleaned = _clean_mcp_schema(schema)
    assert "$schema" not in cleaned
    assert "additionalProperties" not in cleaned
    assert "properties" in cleaned
    assert "$schema" not in cleaned["properties"]["instance_id"]
    assert "additionalProperties" not in cleaned["properties"]["instance_id"]

    # Verify dereferencing of nested $defs and $ref (e.g. PricingFilter in AWS pricing tools)
    schema_with_defs = {
        "type": "object",
        "properties": {
            "service_code": {"type": "string"},
            "filters": {
                "type": "array",
                "items": {"$ref": "#/$defs/PricingFilter"},
            },
        },
        "required": ["service_code"],
        "$defs": {
            "PricingFilter": {
                "type": "object",
                "properties": {
                    "Field": {"type": "string"},
                    "Value": {"type": "string"},
                },
                "required": ["Field", "Value"],
            }
        },
    }
    cleaned_defs = _clean_mcp_schema(schema_with_defs)
    assert "$defs" not in cleaned_defs
    assert "$ref" not in str(cleaned_defs)
    # The referenced definition was inlined directly into properties.filters.items
    items_schema = cleaned_defs["properties"]["filters"]["items"]
    assert items_schema["type"] == "object"
    assert "Field" in items_schema["properties"]
    assert "Value" in items_schema["properties"]

    # Verify circular references and bounded recursion break safely without empty keys or infinite loop
    circular_schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    cleaned_circ = _clean_mcp_schema(circular_schema)
    assert "$defs" not in cleaned_circ
    assert "$ref" not in str(cleaned_circ)
    assert "" not in cleaned_circ
    assert cleaned_circ["properties"]["node"]["properties"]["child"] == {"type": "object"}


def test_is_transient_session_error() -> None:
    assert _is_transient_session_error(BrokenPipeError()) is True
    assert _is_transient_session_error(ConnectionResetError()) is True
    assert _is_transient_session_error(EOFError()) is True
    assert _is_transient_session_error(ValueError("Bad input")) is False


# ── Error Diagnostics ────────────────────────────────────────────────────────


def test_enhance_mcp_error_diagnostics_aws() -> None:
    sso_err = _enhance_mcp_error_diagnostics("botocore.exceptions.ClientError: The security token included in the request is expired (ExpiredToken)")
    assert "aws sso login" in sso_err

    cred_err = _enhance_mcp_error_diagnostics("botocore.exceptions.NoCredentialsError: Unable to locate credentials")
    assert "No AWS credentials found" in cred_err

    denied_err = _enhance_mcp_error_diagnostics("AccessDenied: User is not authorized to perform: ec2:DescribeInstances")
    assert "AWS IAM Access Denied" in denied_err

    endpoint_err = _enhance_mcp_error_diagnostics("EndpointConnectionError: Could not connect to the endpoint URL")
    assert "AWS endpoint URL" in endpoint_err

    crash_err = _enhance_mcp_error_diagnostics("Process failed with Connection closed unexpectedly")
    assert "MCP server process terminated" in crash_err


# ── Pure File-Based Discovery & Precedence ────────────────────────────────────


def test_mcp_discovery_from_files(tmp_path: Path) -> None:
    """Verify pure file-based discovery with no database connection."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aws-cloudwatch": {
                        "command": "uvx",
                        "args": ["mcp-server-aws-cloudwatch"],
                        "env": {"AWS_REGION": "us-west-2"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=tmp_path)
    assert "aws-cloudwatch" in configs
    assert configs["aws-cloudwatch"]["command"] == "uvx"
    assert configs["aws-cloudwatch"]["args"] == ["mcp-server-aws-cloudwatch"]
    assert configs["aws-cloudwatch"]["source"] == "project"
    assert configs["aws-cloudwatch"]["enabled"] is True


def test_mcp_discovery_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify precedence hierarchy: project > user > global."""
    global_dir = tmp_path / "global_agents"
    user_dir = tmp_path / "user_opscloud"
    project_dir = tmp_path / "project_workspace"

    global_dir.mkdir()
    user_dir.mkdir()
    project_dir.mkdir()

    from opscloud.config import paths

    monkeypatch.setattr(paths, "AGENTS_SHARED_DIR", global_dir)
    monkeypatch.setattr(paths, "DATA_DIR", user_dir)
    monkeypatch.setattr(paths, "GLOBAL_MCP_PATH", user_dir / ".mcp.json")

    # 1. Global config (~/.agents/mcp.json)
    (global_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "global-cmd", "env": {"REGION": "global-reg"}},
                    "global-only": {"command": "global-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    # 2. User config (~/.opscloud/.mcp.json)
    (user_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "user-cmd", "env": {"REGION": "user-reg"}},
                    "user-only": {"command": "user-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    # 3. Project config ({project_root}/.mcp.json)
    (project_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "project-cmd", "env": {"REGION": "project-reg"}},
                    "project-only": {"command": "project-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=project_dir)

    # Project wins for shared-server
    assert configs["shared-server"]["command"] == "project-cmd"
    assert configs["shared-server"]["env"]["REGION"] == "project-reg"
    assert configs["shared-server"]["source"] == "project"

    # All tiers are discovered
    assert configs["global-only"]["command"] == "global-only-cmd"
    assert configs["global-only"]["source"] == "global"

    assert configs["user-only"]["command"] == "user-only-cmd"
    assert configs["user-only"]["source"] == "user"

    assert configs["project-only"]["command"] == "project-only-cmd"
    assert configs["project-only"]["source"] == "project"


def test_mcp_trust_store(tmp_path: Path) -> None:
    """Verify SHA-256 fingerprint gating in MCPTrustStore."""
    from opscloud.mcp.trust import MCPTrustStore

    trust_file = tmp_path / "mcp_trust.json"
    trust_store = MCPTrustStore(trust_file=trust_file)

    config_file = tmp_path / ".mcp.json"
    config_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    # Not trusted initially
    assert not trust_store.is_trusted(config_file)

    # Trust it
    trust_store.trust(config_file)
    assert trust_store.is_trusted(config_file)

    # Modify content -> fingerprint invalidation
    config_file.write_text(json.dumps({"mcpServers": {"malicious": {}}}), encoding="utf-8")
    assert not trust_store.is_trusted(config_file)

    # Re-trust
    trust_store.trust(config_file)
    assert trust_store.is_trusted(config_file)

    # Revoke
    trust_store.revoke(config_file)
    assert not trust_store.is_trusted(config_file)


# ── Raw Config Import & Export ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_raw_config_export_and_import(tmp_path: Path) -> None:
    adapter = SqliteConfigAdapter(tmp_path / "test_raw.db")
    store = ConfigStore(adapter)
    await store.initialize()

    raw_json = {
        "mcpServers": {
            "terraform-mcp": {
                "command": "uvx",
                "args": ["terraform-mcp-server"],
                "env": {"TF_LOG": "INFO"},
                "disabledTools": ["terraform_destroy"],
                "disabled": False,
            },
            "remote-aws-proxy": {
                "serverUrl": "http://127.0.0.1:8000/mcp",
                "headers": {"Authorization": "Bearer token123"},
            },
        }
    }

    imported = await import_raw_mcp_config(raw_json, store=store, source="user")
    assert "terraform-mcp" in imported
    assert "remote-aws-proxy" in imported

    db_servers = await store.list_mcp_servers()
    assert len(db_servers) == 2

    exported = export_raw_mcp_config(db_servers)
    assert "mcpServers" in exported
    assert "terraform-mcp" in exported["mcpServers"]
    assert exported["mcpServers"]["terraform-mcp"]["command"] == "uvx"
    assert "remote-aws-proxy" in exported["mcpServers"]
    assert exported["mcpServers"]["remote-aws-proxy"]["serverUrl"] == "http://127.0.0.1:8000/mcp"


# ── Probing and Preload Metadata ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_one_mcp_server_disabled_and_active() -> None:
    # 1. Disabled server
    disabled_conf = {"transport": "stdio", "command": "echo", "enabled": False}
    info_dis = await probe_one_mcp_server("disabled_srv", disabled_conf)
    assert info_dis.status == "disabled"
    assert info_dis.enabled is False
    assert info_dis.tool_count == 0

    # 2. Active server with mocked session
    mock_t1 = MagicMock()
    mock_t1.name = "describe_instances"
    mock_t1.description = "Describe EC2 instances"
    mock_t1.inputSchema = {"type": "object"}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_t1]))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        cfg = {"transport": "http", "url": "http://localhost:8080/mcp", "enabled": True}
        info_act = await probe_one_mcp_server("aws-ec2", cfg)
        assert info_act.status == "ok"
        assert info_act.tool_count == 1
        assert info_act.tools[0].name == "aws-ec2_describe_instances"
        assert info_act.tools[0].original_name == "describe_instances"


def test_mcp_tool_name_provider_safe_length_and_digest() -> None:
    """Verify tool name composition respects provider length limits and hashing."""
    # 1. Short name within limit
    short_name = _mcp_tool_name("aws-ec2", "describe_instances")
    assert short_name == "aws-ec2_describe_instances"
    assert len(short_name) <= 64

    # 2. Sanitization of invalid chars (appends digest to avoid collision)
    dirty_name = _mcp_tool_name("my@server!", "list:buckets?")
    assert dirty_name == "my_server_list_buckets_d02139680706"
    assert len(dirty_name) <= 64

    # 3. Long name with plugin prefix exceeding 64 characters (matching reference/dcode SHA256 digest)
    server_name = "plugin__aws-compute_talkops-devops-plugins_35da5a75__aws-mcp"
    tool_name = "aws___get_tasks"
    hashed_name = _mcp_tool_name(server_name, tool_name)
    assert len(hashed_name) <= 64
    assert hashed_name == "plugin__aws-compute_talkops-devops-_aws___get_tasks_9badd4f791c6"


@pytest.mark.asyncio
async def test_preload_mcp_server_info_integration(tmp_path: Path) -> None:
    clear_cached_mcp_server_infos()

    # Create dummy mcp file
    mcp_file = tmp_path / "custom.mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dummy-server": {
                        "transport": "http",
                        "url": "http://127.0.0.1:9090",
                        "enabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    results = await preload_mcp_server_info(mcp_config_path=str(mcp_file))
    assert len(results) >= 1
    names = [r.name for r in results]
    assert "dummy-server" in names

    cached = get_cached_mcp_server_infos()
    assert any(c.name == "dummy-server" for c in cached)


# ── Semantic Profiler ─────────────────────────────────────────────────────────


def test_semantic_profiler_cloud_tiers() -> None:
    profiler = MCPSemanticProfiler.get_instance()

    # Tier 1 (Read-Only)
    t_read = MagicMock()
    t_read.name = "aws_describe_instances"
    t_read.annotations = None
    p_read = profiler.heuristic_profile(t_read, server_name="aws")
    assert p_read.inferred_tier == 1
    assert p_read.read_only_hint is True
    assert p_read.destructive_hint is False

    # Tier 4 (Destructive - terminate)
    t_dest = MagicMock()
    t_dest.name = "aws_terminate_instances"
    t_dest.annotations = None
    p_dest = profiler.heuristic_profile(t_dest, server_name="aws")
    assert p_dest.inferred_tier == 4
    assert p_dest.read_only_hint is False
    assert p_dest.destructive_hint is True
    assert "instance_id" in p_dest.sensitive_arguments

    # Annotation precedence over name
    t_annot = MagicMock()
    t_annot.name = "safe_delete_preview"
    t_annot.annotations = {"readOnlyHint": True, "destructiveHint": False}
    p_annot = profiler.heuristic_profile(t_annot, server_name="aws")
    # Annotations take precedence if readOnlyHint is true and destructiveHint is not set
    assert p_annot.read_only_hint is True


# ── Middleware & Tools ────────────────────────────────────────────────────────


def test_headless_mcp_guard_middleware() -> None:
    guard = HeadlessMCPGuardMiddleware(tool_names=["aws_terminate_instances"])

    req_blocked = MagicMock()
    req_blocked.tool_call = {"name": "aws_terminate_instances", "id": "call-1"}

    handler = MagicMock()
    result = guard.wrap_tool_call(req_blocked, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "requires approval" in result.content
    handler.assert_not_called()

    req_allowed = MagicMock()
    req_allowed.tool_call = {"name": "aws_describe_instances", "id": "call-2"}
    guard.wrap_tool_call(req_allowed, handler)
    handler.assert_called_once_with(req_allowed)


def test_mcp_context_middleware() -> None:
    server_info = [
        MCPServerInfo(
            name="aws-ec2",
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(name="aws-ec2:describe_instances", description="Describe instances"),
            ),
        )
    ]
    prompt_block = _build_mcp_context_from_infos(server_info)
    assert "**MCP Servers**" in prompt_block
    assert "aws-ec2" in prompt_block
    assert "aws-ec2:describe_instances" in prompt_block


@pytest.mark.asyncio
async def test_cached_mcp_tool_execution() -> None:
    from mcp.types import CallToolResult, TextContent

    mock_mcp_tool = MagicMock()
    mock_mcp_tool.name = "list_buckets"
    mock_mcp_tool.description = "List S3 buckets"
    mock_mcp_tool.inputSchema = {"type": "object", "properties": {}}

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="bucket-1\nbucket-2")],
            isError=False,
        )
    )

    manager = MCPSessionManager({"s3-server": {"transport": "http", "url": "http://localhost:9000"}})
    manager._sessions["s3-server"] = MagicMock(session=mock_session, exit_stack=AsyncMock())

    tool_obj = _build_cached_mcp_tool(
        mcp_tool=mock_mcp_tool,
        server_name="s3-server",
        session_manager=manager,
    )

    assert tool_obj.name == "s3-server:list_buckets"
    result = await tool_obj.ainvoke({})
    assert "bucket-1" in str(result)


@pytest.mark.asyncio
async def test_transient_error_retry_safety() -> None:
    """Verify that read-only tools retry on transient disconnect, but mutating tools fail-safe without replay."""
    from mcp.types import CallToolResult, TextContent
    from langchain_core.tools import ToolException

    # 1. Read-only tool (describe_instances) retries safely
    ro_tool = MagicMock()
    ro_tool.name = "describe_instances"
    ro_tool.description = "Describe instances"
    ro_tool.inputSchema = {"type": "object", "properties": {}}

    mock_ro_session_1 = AsyncMock()
    mock_ro_session_1.call_tool = AsyncMock(side_effect=BrokenPipeError("pipe dead"))
    mock_ro_session_2 = AsyncMock()
    mock_ro_session_2.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="i-123")], isError=False)
    )

    manager = MCPSessionManager({"ec2-server": {"transport": "stdio", "command": "fake"}})
    manager.get_session = AsyncMock(side_effect=[mock_ro_session_1, mock_ro_session_2])
    manager.invalidate = AsyncMock()

    tool_ro = _build_cached_mcp_tool(
        mcp_tool=ro_tool,
        server_name="ec2-server",
        session_manager=manager,
    )
    res = await tool_ro.ainvoke({})
    assert "i-123" in str(res)
    assert manager.invalidate.called

    # 2. Mutating tool (terminate_instances) fails safe without replaying
    mut_tool = MagicMock()
    mut_tool.name = "terminate_instances"
    mut_tool.description = "Terminate instances"
    mut_tool.inputSchema = {"type": "object", "properties": {}}

    mock_mut_session = AsyncMock()
    mock_mut_session.call_tool = AsyncMock(side_effect=BrokenPipeError("socket dead mid-flight"))

    manager_mut = MCPSessionManager({"ec2-server": {"transport": "stdio", "command": "fake"}})
    manager_mut.get_session = AsyncMock(return_value=mock_mut_session)
    manager_mut.invalidate = AsyncMock()

    tool_mut = _build_cached_mcp_tool(
        mcp_tool=mut_tool,
        server_name="ec2-server",
        session_manager=manager_mut,
    )
    res_mut = await tool_mut.ainvoke({})

    assert "Connection lost while calling MCP tool" in str(res_mut)
    assert "not automatically retried" in str(res_mut)
    # Ensure it did not attempt a second call_tool invocation
    assert mock_mut_session.call_tool.call_count == 1


# ── CLI MCP Command ───────────────────────────────────────────────────────────


def test_run_mcp_command_cli() -> None:
    args = argparse.Namespace(action="list", json_output=True, probe=False)
    assert run_mcp_command(args) == 0
