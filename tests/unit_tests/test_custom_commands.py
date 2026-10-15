"""Unit tests for zero-code custom commands and LangChain middleware."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import SystemMessage
import pytest

from opscloud.commands._base import CommandContext
from opscloud.commands._router import CommandRouter
from opscloud.commands.custom import (
    CustomCommandHandler,
    CustomCommandWatcher,
    activate_conditional_commands_for_paths,
    clear_custom_command_cache,
    derive_command_name_from_path,
    discover_custom_commands,
    execute_inline_shell_commands,
    expand_braces,
    expand_custom_command_prompt,
    extract_file_mentions,
    format_command_xml_tags,
    get_command_usage_priority,
    load_custom_command_file,
    parse_custom_command_frontmatter,
    peel_stacked_prompt_commands,
    prepare_forked_command_context,
    record_command_usage,
    resolve_command_with_colon_fallback,
    sanitize_command_arguments,
    substitute_arguments,
)
from opscloud.middleware.custom_commands import (
    CustomCommandsMiddleware,
    create_custom_command_skill_tool,
    is_tool_disallowed,
    is_tool_preapproved,
    matches_tool_permission_rule,
    resolve_command_model_spec,
)
from opscloud.prompts.custom_commands import (
    build_skill_tool_prompt,
    format_commands_for_budget,
)
from opscloud.ui.command_registry import build_custom_commands


# ── 1. Frontmatter, Brace Expansion & Fallback Description ───────────────


def test_expand_braces() -> None:
    expanded = expand_braces("Bash(git {status,diff,log}:*)")
    assert expanded == [
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git log:*)",
    ]


def test_parse_custom_command_frontmatter_full_schema(tmp_path: Path) -> None:
    raw = """---
name: deploy-service
description: Deploy a microservice safely: with dry-run
allowed-tools: Bash(kubectl {get,diff} *), Read(${CLAUDE_SKILL_DIR}/**)
disallowed-tools:
  - Bash(rm *)
  - Write
argument-hint: "[service] [env]"
arguments:
  - service
  - env
model: sonnet
effort: high
context: fork
agent: k8s-specialist
background: true
disable-model-invocation: true
user-invocable: true
when_to_use: Use when deploying Kubernetes services
paths:
  - "k8s/**"
  - "helm/*.yaml"
shell: bash
version: "1.2.0"
---
# Deploy Heading
Deploy $service to $env environment.
"""
    fm, body, desc, has_user_desc = parse_custom_command_frontmatter(
        raw,
        skill_dir=tmp_path / "skill_dir",
        project_dir=tmp_path,
    )
    assert has_user_desc is True
    assert desc == "Deploy a microservice safely: with dry-run"
    assert fm.name == "deploy-service"
    assert "Bash(kubectl get *)" in fm.allowed_tools
    assert "Bash(kubectl diff *)" in fm.allowed_tools
    assert f"Read({(tmp_path / 'skill_dir').as_posix()}/**)" in fm.allowed_tools
    assert fm.disallowed_tools == ["Bash(rm *)", "Write"]
    assert fm.argument_hint == "[service] [env]"
    assert fm.arguments == ["service", "env"]
    assert fm.model == "sonnet"
    assert fm.effort == "high"
    assert fm.context == "fork"
    assert fm.agent == "k8s-specialist"
    assert fm.background is True
    assert fm.disable_model_invocation is True
    assert fm.user_invocable is True
    assert fm.when_to_use == "Use when deploying Kubernetes services"
    assert fm.paths == ["k8s", "helm/*.yaml"]
    assert fm.shell == "bash"
    assert fm.version == "1.2.0"
    assert "Deploy $service to $env" in body


def test_frontmatter_markdown_heading_fallback() -> None:
    raw = """# Automated PR Security Audit

Check the diff for secrets and IAM wildcards.
"""
    fm, body, desc, has_user_desc = parse_custom_command_frontmatter(raw)
    assert has_user_desc is False
    assert desc == "Automated PR Security Audit"
    assert fm.user_invocable is True
    assert fm.context == "inline"


# ── 2. Argument Substitution, Sanitization & Inline Shell Execution ──────


def test_sanitize_command_arguments_prevents_shell_injection() -> None:
    malicious = "foo !`rm -rf /` `!whoami` !ls"
    sanitized = sanitize_command_arguments(malicious)
    assert "!`" not in sanitized
    assert "`!" not in sanitized
    assert r"\!ls" in sanitized


def test_substitute_arguments_named_positional_and_auto_append() -> None:
    # Named and indexed positional replacement with escaped \$
    template = r"Service=$service ($0 / $ARGUMENTS[1]), cost=\$100, all=$ARGUMENTS"
    res = substitute_arguments(
        template,
        'payments "us-east-1"',
        argument_names=["service", "region"],
    )
    assert res == "Service=payments (payments / us-east-1), cost=$100, all=payments \"us-east-1\""

    # Auto-append ARGUMENTS: when no placeholder exists in template
    no_placeholder = "Review the following changes carefully."
    res_auto = substitute_arguments(no_placeholder, "src/main.py --strict")
    assert res_auto == "Review the following changes carefully.\n\nARGUMENTS: src/main.py --strict"


def test_execute_inline_shell_commands_skips_regular_code_blocks(tmp_path: Path) -> None:
    template = """Current branch: !`printf "main-branch"`

```!
printf "from-fenced-shell"
```

Regular code block should NOT run:
```bash
echo !`printf "should-not-run"`
```
And inline code ``!`printf "nope"` `` is untouched.
"""
    expanded = execute_inline_shell_commands(template, cwd=tmp_path)
    assert "Current branch: main-branch" in expanded
    assert "from-fenced-shell" in expanded
    assert 'echo !`printf "should-not-run"`' in expanded
    assert "should-not-run" not in expanded.replace('echo !`printf "should-not-run"`', "")


def test_full_expand_custom_command_prompt(tmp_path: Path) -> None:
    cmd_dir = tmp_path / ".opscloud" / "commands"
    cmd_dir.mkdir(parents=True)
    cmd_file = cmd_dir / "check.md"
    cmd_file.write_text(
        """---
description: Check target module
arguments: [target]
allowed-tools: Read, Bash(git *)
model: haiku
effort: low
---
Inspecting $target in ${CLAUDE_PROJECT_DIR} (session ${CLAUDE_SESSION_ID}, effort ${CLAUDE_EFFORT}).
Shell check: !`printf "ok"`
See @src/opscloud/agent/factory.py for details.
""",
        encoding="utf-8",
    )

    defn = load_custom_command_file(cmd_file, cmd_dir, source="projectSettings", project_dir=tmp_path)
    assert defn is not None
    expanded = expand_custom_command_prompt(
        defn,
        "auth-module",
        cwd=tmp_path,
        project_dir=tmp_path,
        session_id="sess-123",
    )
    assert f"Inspecting auth-module in {tmp_path.as_posix()} (session sess-123, effort low)." in expanded.expanded_prompt
    assert "Shell check: ok" in expanded.expanded_prompt
    assert "src/opscloud/agent/factory.py" in expanded.referenced_files
    assert "<command-message>check</command-message>" in expanded.xml_command_message
    assert "<command-name>/check</command-name>" in expanded.xml_command_message
    assert "<command-args>auth-module</command-args>" in expanded.xml_command_message
    assert expanded.permissions.allowed_tools == ["Read", "Bash(git *)"]
    assert expanded.permissions.model == "haiku"
    assert expanded.permissions.effort == "low"


# ── 3. Multi-Tier Discovery, Colon-Namespacing, Conditional Paths & Watcher ──


def test_subdirectory_colon_namespacing_and_skill_md_priority(tmp_path: Path) -> None:
    clear_custom_command_cache(reset_conditional=True)
    cmd_root = tmp_path / ".claude" / "commands"
    (cmd_root / "frontend").mkdir(parents=True)
    (cmd_root / "frontend" / "component.md").write_text("# Create React component", encoding="utf-8")

    # Directory with SKILL.md should prioritize SKILL.md and ignore sibling .md
    skill_sub = cmd_root / "devops" / "k8s"
    skill_sub.mkdir(parents=True)
    (skill_sub / "SKILL.md").write_text(
        "---\ndescription: Kubernetes cluster ops\n---\nRun kubectl.",
        encoding="utf-8",
    )
    (skill_sub / "ignored_sibling.md").write_text("# Should be skipped", encoding="utf-8")

    discovered = discover_custom_commands(
        cwd=tmp_path,
        project_root=tmp_path,
        include_plugins=False,
        use_cache=False,
    )
    by_name = {c.name: c for c in discovered}
    assert "frontend:component" in by_name
    assert "devops:k8s" in by_name
    assert "devops:k8s:ignored_sibling" not in by_name
    assert by_name["devops:k8s"].base_dir == skill_sub


def test_conditional_paths_activation_and_watcher(tmp_path: Path) -> None:
    clear_custom_command_cache(reset_conditional=True)
    cmd_root = tmp_path / ".opscloud" / "commands"
    cmd_root.mkdir(parents=True)

    (cmd_root / "always.md").write_text("---\ndescription: Always active\n---\nAlways.", encoding="utf-8")
    (cmd_root / "tf-only.md").write_text(
        "---\ndescription: Terraform rules\npaths:\n  - 'infra/**/*.tf'\n---\nUse terraform fmt.",
        encoding="utf-8",
    )

    watcher = CustomCommandWatcher(cwd=tmp_path, project_root=tmp_path)
    assert watcher.check_and_reload() is False

    # Initially only unconditional command is active
    active_before = discover_custom_commands(
        cwd=tmp_path,
        project_root=tmp_path,
        include_plugins=False,
        include_conditional=False,
        use_cache=False,
    )
    names_before = {c.name for c in active_before}
    assert "always" in names_before
    assert "tf-only" not in names_before

    # Touching a matching file dynamically activates tf-only
    activated = activate_conditional_commands_for_paths(
        ["infra/modules/vpc/main.tf"],
        cwd=tmp_path,
        project_root=tmp_path,
    )
    assert [c.name for c in activated] == ["tf-only"]

    active_after = discover_custom_commands(
        cwd=tmp_path,
        project_root=tmp_path,
        include_plugins=False,
        include_conditional=False,
        use_cache=False,
    )
    assert "tf-only" in {c.name for c in active_after}

    # Modifying a command file triggers CustomCommandWatcher reload
    (cmd_root / "always.md").write_text("---\ndescription: Updated description\n---\nUpdated.", encoding="utf-8")
    assert watcher.check_and_reload() is True
    assert watcher.check_and_reload() is False


# ── 4. Router Space-to-Colon Fallback, Stacked Commands & Fork Context ──


@pytest.mark.asyncio
async def test_router_space_to_colon_and_stacked_commands(tmp_path: Path) -> None:
    clear_custom_command_cache(reset_conditional=True)
    cmd_root = tmp_path / ".opscloud" / "commands"
    (cmd_root / "frontend").mkdir(parents=True)
    (cmd_root / "frontend" / "component.md").write_text(
        "---\ndescription: Build UI component\nallowed-tools: Read, Write\n---\nComponent: $ARGUMENTS",
        encoding="utf-8",
    )
    (cmd_root / "lint.md").write_text(
        "---\ndescription: Run linter\nallowed-tools: Bash(ruff *)\ndisallowed-tools: Write\n---\nLinting $ARGUMENTS",
        encoding="utf-8",
    )
    (cmd_root / "forked-audit.md").write_text(
        "---\ndescription: Audit in subagent\ncontext: fork\nagent: security-auditor\n---\nAudit $ARGUMENTS",
        encoding="utf-8",
    )

    defs = discover_custom_commands(cwd=tmp_path, project_root=tmp_path, include_plugins=False, use_cache=False)
    router = CommandRouter()
    for d in defs:
        router.register(CustomCommandHandler(d))

    # 1. Space-to-colon fallback: `/frontend component Button` -> `/frontend:component` with args `Button`
    ctx = CommandContext(app=None, raw_command="/frontend component Button", args="component Button")
    res = await router.dispatch("/frontend component Button", ctx)
    assert res.success is True
    assert res.data["command_name"] == "frontend:component"
    assert "Component: Button" in res.data["custom_command_prompt"]

    # 2. Stacked prompt commands: `/lint /frontend:component Card.tsx`
    ctx_stack = CommandContext(app=None, raw_command="/lint /frontend:component Card.tsx", args="")
    res_stack = await router.dispatch("/lint /frontend:component Card.tsx", ctx_stack)
    assert res_stack.success is True
    assert res_stack.data["stacked_commands"] == ["lint", "frontend:component"]
    assert "Linting" in res_stack.data["custom_command_prompt"]
    assert "Component: Card.tsx" in res_stack.data["custom_command_prompt"]
    assert set(res_stack.data["allowed_tools"]) == {"Bash(ruff *)", "Read", "Write"}
    assert res_stack.data["disallowed_tools"] == ["Write"]

    # 3. Forked execution context (`context: fork`)
    ctx_fork = CommandContext(app=None, raw_command="/forked-audit src/", args="src/")
    res_fork = await router.dispatch("/forked-audit src/", ctx_fork)
    assert res_fork.success is True
    assert res_fork.data["execution_context"] == "fork"
    assert res_fork.data["forked_context"]["agent_type"] == "security-auditor"
    assert res_fork.data["forked_context"]["_forked_command_turn"] is True


# ── 5. LangChain CustomCommandsMiddleware & Budgeted Prompts ────────────


def test_middleware_permissions_model_override_and_turn_reset(tmp_path: Path) -> None:
    clear_custom_command_cache(reset_conditional=True)
    cmd_root = tmp_path / ".opscloud" / "commands"
    cmd_root.mkdir(parents=True)
    (cmd_root / "deploy.md").write_text(
        "---\ndescription: Deploy command\nwhen_to_use: When shipping to prod\n---\nDeploy.",
        encoding="utf-8",
    )

    sentinel_model = MagicMock(name="resolved_sonnet_model")
    mw = CustomCommandsMiddleware(
        cwd=tmp_path,
        project_root=tmp_path,
        enforce_allowed_tools=True,
        model_resolver=lambda spec: sentinel_model if "sonnet" in spec else None,
    )

    # Turn 1: Custom command turn with allowed-tools, disallowed-tools, model, effort, and fork
    state_turn_1: dict[str, Any] = {
        "custom_command_request": {
            "command_name": "deploy",
            "allowed_tools": ["Bash(git *)", "Read"],
            "disallowed_tools": ["Bash(rm *)", "write_file"],
            "model": "sonnet",
            "effort": "high",
            "execution_context": "fork",
            "custom_command_prompt": "Deploy now",
        }
    }
    update_1 = mw.before_agent(state_turn_1)
    assert update_1 is not None
    active_perms = update_1["_active_command_permissions"]
    assert active_perms["allowed_tools"] == ["Bash(git *)", "Read"]
    assert active_perms["disallowed_tools"] == ["Bash(rm *)", "write_file"]
    assert active_perms["model"] == resolve_command_model_spec("sonnet")
    assert active_perms["effort"] == "high"
    assert update_1["_forked_command_turn"] is True
    assert "main:deploy" in update_1["_invoked_custom_commands"]

    # Verify wrap_model_call applies model, effort, tool filtering (including forked subagent guard), and system prompt
    merged_state = {**state_turn_1, **update_1}
    mock_request = MagicMock()
    mock_request.state = merged_state
    mock_request.model_settings = {}
    mock_request.tools = [
        {"name": "read_file"},
        {"name": "write_file"},
        {"name": "task"},
        {"name": "execute"},
    ]
    mock_request.system_message = SystemMessage(content="Base system prompt.")

    def _override(**kwargs: Any) -> MagicMock:
        overridden = MagicMock()
        overridden.overrides = kwargs
        return overridden

    mock_request.override.side_effect = _override
    captured_req: Any = mw._prepare_model_request(mock_request)
    overrides = captured_req.overrides
    assert overrides["model"] is sentinel_model
    assert "model_settings" not in overrides
    # `write_file` (disallowed) and `task` (blocked on forked turns) are filtered out
    remaining_tool_names = [t["name"] for t in overrides["tools"]]
    assert "read_file" in remaining_tool_names
    assert "execute" in remaining_tool_names
    assert "write_file" not in remaining_tool_names
    assert "task" not in remaining_tool_names
    assert "<custom_commands>" in str(overrides["system_message"].content)
    assert "- deploy: Deploy command - When shipping to prod" in str(overrides["system_message"].content)

    # Verify wrap_tool_call blocks parameterized `Bash(rm *)` and allows `Bash(git status)`
    rm_req = MagicMock()
    rm_req.state = merged_state
    rm_req.tool_call = {"name": "execute", "args": {"command": "rm -rf /tmp/foo"}, "id": "call_rm"}
    rm_res_any: Any = "ran"
    rm_res = mw.wrap_tool_call(rm_req, lambda r: rm_res_any)
    assert getattr(rm_res, "status", None) == "error"
    assert "blocked" in str(getattr(rm_res, "content", "")).lower()

    git_req = MagicMock()
    git_req.state = merged_state
    git_req.tool_call = {"name": "execute", "args": {"command": "git status"}, "id": "call_git"}
    git_res_any: Any = "git_ok"
    git_res = mw.wrap_tool_call(git_req, lambda r: git_res_any)
    assert git_res == "git_ok"

    # Turn 2: Normal user turn clears turn-scoped `disallowed_tools`, `model`, `effort`, and `_forked_command_turn`
    # while preserving session `allowed_tools`
    state_turn_2: Any = {
        "_active_command_permissions": active_perms,
        "_forked_command_turn": True,
    }
    update_2 = mw.before_agent(state_turn_2)
    assert update_2 is not None
    assert update_2["_forked_command_turn"] is False
    assert update_2["_active_command_permissions"]["allowed_tools"] == ["Bash(git *)", "Read"]
    assert update_2["_active_command_permissions"]["disallowed_tools"] == []
    assert update_2["_active_command_permissions"]["model"] is None
    assert update_2["_active_command_permissions"]["effort"] is None


def test_skill_tool_and_usage_priority_budgeting(tmp_path: Path) -> None:
    clear_custom_command_cache(reset_conditional=True)
    cmd_root = tmp_path / ".opscloud" / "commands"
    cmd_root.mkdir(parents=True)
    (cmd_root / "alpha.md").write_text("---\ndescription: Alpha command\n---\nAlpha $ARGUMENTS", encoding="utf-8")
    (cmd_root / "beta.md").write_text(
        "---\ndescription: Beta command\ndisable-model-invocation: true\n---\nBeta",
        encoding="utf-8",
    )

    usage_file = tmp_path / "usage.json"
    record_command_usage("alpha", usage_path=usage_file)
    record_command_usage("alpha", usage_path=usage_file)
    assert get_command_usage_priority("alpha", usage_path=usage_file) > 1.5

    cmds = discover_custom_commands(cwd=tmp_path, project_root=tmp_path, include_plugins=False, use_cache=False)
    prompt_str = build_skill_tool_prompt(cmds, usage_path=usage_file)
    assert "- alpha: Alpha command" in prompt_str
    # beta has disable-model-invocation: true so it is excluded from Skill tool listing
    assert "- beta:" not in prompt_str

    skill_tool = create_custom_command_skill_tool(cwd=tmp_path, project_root=tmp_path)
    out_alpha = skill_tool.invoke({"skill": "alpha", "args": "hello"})
    assert "<command-name>/alpha</command-name>" in out_alpha
    assert "Alpha hello" in out_alpha

    out_beta = skill_tool.invoke({"skill": "beta", "args": ""})
    assert "disable-model-invocation=true" in out_beta

    ui_entries = build_custom_commands(cmds)
    assert any(e.name == "/alpha" for e in ui_entries)


def test_forked_command_isolates_messages_and_expands_anthropic_cache_control(tmp_path: Path) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
    from langchain_core.messages import AIMessage, HumanMessage

    from opscloud.model.factory import _ensure_anthropic_block_level_cache_control

    _ensure_anthropic_block_level_cache_control()
    anthropic_kwargs: dict[str, Any] = {"model": "claude-sonnet-4-5", "api_key": "test-key"}
    chat_anthropic = ChatAnthropic(**anthropic_kwargs)

    mw = CustomCommandsMiddleware(
        cwd=tmp_path,
        project_root=tmp_path,
        inject_system_prompt=False,
        model_resolver=lambda _spec: chat_anthropic,
    )
    state = {
        "_active_command_permissions": {
            "command_name": "deep-check",
            "allowed_tools": [],
            "disallowed_tools": [],
            "model": "anthropic:claude-sonnet-4-5",
            "effort": "high",
            "execution_context": "fork",
        },
        "_forked_command_turn": True,
    }
    mock_request = MagicMock()
    mock_request.state = state
    mock_request.model_settings = {}
    mock_request.messages = [
        HumanMessage(content="Prior turn user message"),
        AIMessage(content="Prior turn assistant response"),
        HumanMessage(content="<command-name>/deep-check</command-name>\n\nCheck architecture"),
    ]
    mock_request.tools = [{"name": "read_file"}, {"name": "Skill"}, {"name": "task"}]
    mock_request.system_message = SystemMessage(content="System prompt")

    def _override(**kwargs: Any) -> MagicMock:
        overridden = MagicMock()
        overridden.overrides = kwargs
        overridden.model = kwargs.get("model", chat_anthropic)
        overridden.messages = kwargs.get("messages", mock_request.messages)
        overridden.system_message = kwargs.get("system_message", mock_request.system_message)
        overridden.tools = kwargs.get("tools", mock_request.tools)
        overridden.model_settings = kwargs.get("model_settings", {})
        overridden.override.side_effect = _override
        return overridden

    mock_request.override.side_effect = _override
    prepared: Any = mw._prepare_model_request(mock_request)

    # Forked command turn isolates messages to the current forked command turn only
    assert len(prepared.messages) == 1
    assert "/deep-check" in str(prepared.messages[0].content)
    # Both `Skill` and `task` are filtered out in forked turns
    assert [t["name"] for t in prepared.tools] == ["read_file"]

    # AnthropicPromptCachingMiddleware adds `cache_control` to `model_settings`,
    # and `ChatAnthropic._get_request_payload` expands it to a block-level breakpoint
    # without leaving an invalid top-level `cache_control` key in the wire payload.
    caching_mw = AnthropicPromptCachingMiddleware(
        unsupported_model_behavior="ignore",
        min_messages_to_cache=1,
    )
    cached_req: Any = caching_mw._apply_caching(prepared)
    assert "cache_control" in cached_req.model_settings

    payload_msgs: Any = [cached_req.system_message, *cached_req.messages]
    wire_payload = chat_anthropic._get_request_payload(
        payload_msgs,
        **cached_req.model_settings,
    )
    assert "cache_control" not in wire_payload
    last_block = wire_payload["messages"][-1]["content"][-1]
    assert last_block["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

