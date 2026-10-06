"""Unit tests for OpsCloud prompts generation and typed interrupt schemas."""

from pathlib import Path
import re

from opscloud.prompts import (
    CloudProvider,
    MODEL_IDENTITY_RE,
    OPSCLOUD_MEMORY_HEADLESS_SYSTEM_PROMPT,
    OPSCLOUD_MEMORY_READONLY_SYSTEM_PROMPT,
    OPSCLOUD_MEMORY_SYSTEM_PROMPT,
    build_fs_tool_guidance,
    build_model_identity_section,
    build_working_dir_section,
    get_base_system_prompt,
    get_memory_system_prompt,
    normalize_cloud_provider,
)
from opscloud.schema.interrupts import HitlResumePayload


def test_build_model_identity_section():
    section = build_model_identity_section(
        name="claude-3-7-sonnet",
        provider="anthropic",
        context_limit=200_000,
        unsupported_modalities=frozenset(["audio"]),
    )
    assert "### Model Identity" in section
    assert "claude-3-7-sonnet" in section
    assert "anthropic" in section
    assert "200,000" in section
    assert "Audio" in section


def test_build_model_identity_empty():
    assert build_model_identity_section(None) == ""
    assert build_model_identity_section("") == ""


def test_normalize_cloud_provider():
    assert normalize_cloud_provider(None) == CloudProvider.AWS
    assert normalize_cloud_provider("aws") == CloudProvider.AWS
    assert normalize_cloud_provider("amazon") == CloudProvider.AWS
    assert normalize_cloud_provider("azure") == CloudProvider.AZURE
    assert normalize_cloud_provider("az") == CloudProvider.AZURE
    assert normalize_cloud_provider("gcp") == CloudProvider.GCP
    assert normalize_cloud_provider("google") == CloudProvider.GCP
    assert normalize_cloud_provider("multi") == CloudProvider.MULTI
    assert normalize_cloud_provider("unknown_provider") == CloudProvider.AWS


def test_get_base_system_prompt_aws_default():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="gpt-4o",
    )
    assert len(prompt) > 200
    assert "Multi-Agent Orchestrator" in prompt
    assert "Skill-Based Multi-Agent Framework" in prompt
    assert "write_todos" in prompt
    assert "aws-core" in prompt
    assert "aws-terraform" in prompt
    assert "aws-eks-autopilot" in prompt
    assert "Computational Verification" in prompt
    assert "Blast Radius Safeguards" in prompt
    assert "Preserving Subagent Technical Deliverables" in prompt


def test_get_base_system_prompt_azure():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="claude-3-7-sonnet",
        cloud_provider=CloudProvider.AZURE,
    )
    assert "Cloud Platform Context: Azure" in prompt
    assert "azure-ops" in prompt
    assert "az account show" in prompt


def test_get_base_system_prompt_gcp():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="gemini-2.5-pro",
        cloud_provider=CloudProvider.GCP,
    )
    assert "Cloud Platform Context: Google Cloud (GCP)" in prompt
    assert "gcp-ops" in prompt
    assert "Workload Identity" in prompt


def test_get_base_system_prompt_multi():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="claude-3-5-sonnet",
        cloud_provider=CloudProvider.MULTI,
    )
    assert "Cloud Platform Context: Multi-Cloud" in prompt
    assert "multi-cloud-ops" in prompt
    assert "workload portability" in prompt.lower()


def test_get_base_system_prompt_headless():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=False,
        model_name="claude-3-5-sonnet",
    )
    assert len(prompt) > 100
    assert "non-interactive (headless) mode" in prompt
    assert "Do NOT ask clarifying questions" in prompt
    assert "Never run commands that block waiting for stdin" in prompt


def test_no_unreplaced_placeholders():
    for provider in [CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP, CloudProvider.MULTI]:
        for interactive in [True, False]:
            prompt = get_base_system_prompt(
                assistant_id="opscloud",
                interactive=interactive,
                cwd="/tmp/test_workspace",
                fs_tools=["read_file", "write_file", "edit_file"],
                model_name="claude-3-7-sonnet",
                model_provider="anthropic",
                model_context_limit=200_000,
                cloud_provider=provider,
                sandbox_type=None,
                has_search=True,
            )
            unreplaced = re.findall(r"\{[a-z_]+\}", prompt)
            assert not unreplaced, f"Found unreplaced placeholders for {provider} (interactive={interactive}): {unreplaced}"


def test_model_identity_hotpatch_regex():
    initial_prompt = get_base_system_prompt(
        model_name="gpt-4o",
        model_provider="openai",
        model_context_limit=128_000,
    )
    assert "model `gpt-4o` (provider: openai)" in initial_prompt

    new_identity = build_model_identity_section(
        name="claude-3-7-sonnet",
        provider="anthropic",
        context_limit=200_000,
    )
    patched = MODEL_IDENTITY_RE.sub(new_identity, initial_prompt, count=1)
    assert "model `claude-3-7-sonnet` (provider: anthropic)" in patched
    assert "gpt-4o" not in patched
    assert "Multi-Agent Orchestrator" in patched


def test_sandbox_working_dir_guidance():
    # Local mode
    local_section = build_working_dir_section(cwd="/Users/operator/project", sandbox_type=None)
    assert "/Users/operator/project" in local_section
    assert "remote Linux sandbox" not in local_section

    # Remote sandbox mode
    sandbox_section = build_working_dir_section(cwd="/workspace", sandbox_type="daytona")
    assert "**remote Linux sandbox** (`daytona`)" in sandbox_section
    assert "You do NOT have access to the operator's local filesystem" in sandbox_section


def test_fs_tool_guidance():
    # Restricted tool set
    guidance = build_fs_tool_guidance(["read_file", "write_file"])
    assert "You have restricted access to the filesystem" in guidance
    assert "`edit_file`" not in guidance

    # Full set
    full_guidance = build_fs_tool_guidance(None)
    assert "edit_file" in full_guidance
    assert "write_file" in full_guidance


def test_memory_system_prompts():
    default_prompt = get_memory_system_prompt()
    assert default_prompt == OPSCLOUD_MEMORY_SYSTEM_PROMPT
    assert "live cloud telemetry and explicit operator commands" in default_prompt.lower()

    readonly_prompt = get_memory_system_prompt(readonly=True)
    assert readonly_prompt == OPSCLOUD_MEMORY_READONLY_SYSTEM_PROMPT
    assert "Automatic memory saving is disabled" in readonly_prompt

    headless_prompt = get_memory_system_prompt(headless=True)
    assert headless_prompt == OPSCLOUD_MEMORY_HEADLESS_SYSTEM_PROMPT
    assert "Working autonomously" in headless_prompt


def test_built_in_skills_exist_and_valid():
    built_in_dir = Path(__file__).parent.parent.parent / "src" / "opscloud" / "built_in_skills"
    assert built_in_dir.is_dir()

    expected_skills = [
        "remember",
        "skill-creator",
    ]

    for skill_name in expected_skills:
        skill_file = built_in_dir / skill_name / "SKILL.md"
        assert skill_file.exists(), f"Missing SKILL.md for {skill_name}"
        content = skill_file.read_text(encoding="utf-8")
        assert f"name: {skill_name}" in content
        assert "description:" in content


def test_hitl_resume_payload_parsing():
    payload_approve = HitlResumePayload.from_raw("approve")
    assert len(payload_approve.decisions) == 1
    assert payload_approve.decisions[0].type == "approve"
    assert payload_approve.auto_approve_requested is False

    payload_reject = HitlResumePayload.from_raw("reject")
    assert len(payload_reject.decisions) == 1
    assert payload_reject.decisions[0].type == "reject"

    payload_auto = HitlResumePayload.from_raw({"decisions": [{"type": "auto"}]})
    assert payload_auto.auto_approve_requested is True
    assert payload_auto.decisions[0].type == "approve"


def test_get_base_system_prompt_dual_engine_and_tool_discipline():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="claude-3-7-sonnet",
    )
    # Validate Dual-Engine Architecture
    assert "Dual-Engine Architecture" in prompt
    assert "Platform Engineering & DevOps Coding Specialist" in prompt
    assert "Multi-Agent Cloud Operations & Orchestrator" in prompt
    assert "Infrastructure as Code (IaC)" in prompt
    assert "Cloud-Native & Container Orchestration" in prompt
    assert "CI/CD & GitOps Automation" in prompt

    # Validate Cloud Foundation & Plugin Marketplace Ecosystem
    assert "Native Cloud Foundation (AWS)" in prompt
    assert "Extensible Plugin Marketplace Ecosystem" in prompt

    # Validate Tool-Specific Output Discipline
    assert "Tool Usage & Tool-Specific Output Discipline" in prompt
    assert "`read_file`" in prompt
    assert "`edit_file`" in prompt
    assert "`write_file`" in prompt
    assert "`task` & `js_eval` (Subagent Delegation)" in prompt

    # Ensure stale draft phrasing was eliminated
    assert "(primarily AWS for this release" not in prompt


def test_adaptive_output_granularity_contract():
    prompt = get_base_system_prompt(
        assistant_id="opscloud",
        interactive=True,
        model_name="claude-3-7-sonnet",
    )
    assert "Adaptive Output Granularity Contract" in prompt
    assert "Conversational & Capability Inquiries" in prompt
    assert "DO NOT output an exhaustive laundry list" in prompt
    assert "Artifact vs Inline Output Separation" in prompt

