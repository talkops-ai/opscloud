"""Unit tests for OpsCloud memory enhancements, MemoryStore, and prompt verification."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage

from opscloud.agent.factory import create_opscloud_agent
from opscloud.config.paths import (
    DEFAULT_AGENT_MD_TEMPLATE,
    ensure_user_agent_md,
    primary_agent_md,
    user_agent_md,
)
from opscloud.memory.onboarding import (
    ONBOARDING_NAME_MEMORY_END,
    ONBOARDING_NAME_MEMORY_START,
    _onboarding_name_memory_block,
    _upsert_onboarding_name_memory,
    extract_onboarding_name_block,
    run_onboarding_if_needed,
    strip_onboarding_name_markers,
)
from opscloud.memory.store import MemoryEntry, MemoryStore, _sanitize_key


captured_model_inputs: list[Any] = []


class ToolCallingFakeModel(FakeListChatModel):
    """Fake model implementing bind_tools for agent invocation tests."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> ToolCallingFakeModel:
        return self

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        captured_model_inputs.append(input)
        return await super().ainvoke(input, *args, **kwargs)


def test_sanitize_key():
    assert _sanitize_key("Prefer Terraform Over OpenTofu") == "prefer-terraform-over-opentofu"
    assert _sanitize_key("   AWS-EKS-Tips!!!   ") == "aws-eks-tips"
    assert _sanitize_key("") == "memory"


def test_memory_store_crud(tmp_path: Path):
    proj_dir = tmp_path / "project"
    user_home = tmp_path / "user"
    store = MemoryStore(project_root=proj_dir, user_home=user_home)

    # 1. Save user memory
    path_u = store.save("cloud-target", "Use AWS us-east-1", scope="user")
    assert path_u.exists()
    assert (user_home / ".opscloud" / "memory" / "cloud-target.md").exists()

    # 2. Save project memory (overrides same key)
    path_p = store.save("cloud-target", "Use AWS us-west-2 for this project", scope="project")
    assert path_p.exists()
    assert (proj_dir / ".opscloud" / "memory" / "cloud-target.md").exists()

    # 3. Get should return project memory due to precedence
    entry = store.get("cloud-target")
    assert entry is not None
    assert entry.source == "project"
    assert "us-west-2" in entry.content

    # 4. List all
    store.save("naming-convention", "Use kebab-case for S3 buckets", scope="user")
    entries = store.list_all()
    assert len(entries) == 2
    keys = [e.key for e in entries]
    assert "cloud-target" in keys
    assert "naming-convention" in keys

    # 5. Load context string
    ctx = store.load_context()
    assert "# Persisted Memories" in ctx
    assert "## Memory: cloud-target" in ctx
    assert "## Memory: naming-convention" in ctx

    # 6. Delete
    deleted = store.delete("naming-convention")
    assert deleted is True
    assert store.get("naming-convention") is None


def test_onboarding_helpers():
    block = _onboarding_name_memory_block(
        name="Lead SRE",
        provider="anthropic",
        cloud="AWS",
        region="us-east-1",
        iac="Terraform",
    )
    assert ONBOARDING_NAME_MEMORY_START in block
    assert ONBOARDING_NAME_MEMORY_END in block
    assert 'The user\'s role/name is "Lead SRE".' in block

    extracted = extract_onboarding_name_block(block)
    assert extracted == block

    stripped = strip_onboarding_name_markers(block)
    assert ONBOARDING_NAME_MEMORY_START not in stripped
    assert "Lead SRE" in stripped

    # Test upsert
    upserted = _upsert_onboarding_name_memory("Existing notes", block)
    assert "Existing notes" in upserted
    assert "Lead SRE" in upserted

    # Replace existing block
    new_block = _onboarding_name_memory_block(name="Principal Platform Architect")
    replaced = _upsert_onboarding_name_memory(upserted, new_block)
    assert "Principal Platform Architect" in replaced
    assert "Lead SRE" not in replaced


def test_ensure_user_agent_md_seeds_default(tmp_path: Path, monkeypatch):
    fake_data_dir = tmp_path / ".opscloud"
    monkeypatch.setattr("opscloud.config.paths.DATA_DIR", fake_data_dir)
    monkeypatch.setattr(
        "opscloud.config.paths.agent_dir",
        lambda name="opscloud": fake_data_dir / name,
    )

    primary_md = ensure_user_agent_md("opscloud")
    assert primary_md.exists()
    assert primary_md.stat().st_size > 0
    content = primary_md.read_text(encoding="utf-8")
    assert "OpsCloud Persistent Memory & Operational Context" in content
    assert "Cloud Platform & DevOps Engineer" in content
    assert "AWS" in content


def test_no_dual_memory_for_default_agent(tmp_path: Path, monkeypatch):
    """Verify that default agent resolves exactly one user AGENTS.md without opscloud/opscloud duplication."""
    fake_data_dir = tmp_path / ".opscloud"
    monkeypatch.setattr("opscloud.config.paths.DATA_DIR", fake_data_dir)

    from opscloud.memory.registry import MemoryRegistry
    registry = MemoryRegistry.get_instance()
    sources = registry.get_all_memory_sources(project_root=tmp_path / "proj")
    user_sources = [s for s in sources if str(fake_data_dir) in s]
    assert len(user_sources) == 1
    assert user_sources[0] == str((fake_data_dir / "AGENTS.md").resolve())


@pytest.mark.asyncio
async def test_agent_system_prompt_audit():
    """Verify that create_opscloud_agent produces a complete, accurate system prompt."""
    fake_model = ToolCallingFakeModel(responses=["Operational greetings."])
    agent, backend = create_opscloud_agent(model=fake_model)

    captured_model_inputs.clear()
    await agent.ainvoke({"messages": [HumanMessage(content="hi how are you")]})
    assert len(captured_model_inputs) >= 1

    system_msg = captured_model_inputs[0][0]
    prompt_text = system_msg.content

    # 1. Subagent Delegation Architecture is present
    assert "## Subagent Delegation & Orchestration Architecture" in prompt_text
    assert "general-purpose" in prompt_text

    # 2. Memory context is loaded from AGENTS.md (NOT "(No memory loaded)")
    assert "<agent_memory>" in prompt_text
    assert "(No memory loaded)" not in prompt_text
    assert "OpsCloud Persistent Memory & Operational Context" in prompt_text
    assert "User Preferences & Defaults" in prompt_text

    # 3. No false filesystem restriction
    assert "You have restricted access to the filesystem. Only the following file tools are available: fetch_url." not in prompt_text

    # 4. Clean skills path formatting without mismatched backticks
    assert "`~/.opscloud/skills` or project-level `.opscloud/skills`" in prompt_text
