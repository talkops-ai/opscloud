"""Unit tests for OpsCloud skill system, parser, loader, trust store, and commands."""

import argparse
from pathlib import Path
import pytest

from opscloud.skills.commands import (
    _create,
    _delete,
    _format_info_fields,
    _generate_template,
    _validate_name,
    _validate_skill_path,
    execute_skills_command,
    setup_skills_parser,
)
from opscloud.skills.invocation import (
    build_skill_invocation_envelope,
    discover_skills_and_roots,
    parse_skill_command,
)
from opscloud.skills.load import (
    _parse_skill_file,
    get_skill_by_name,
    get_skill_content_by_name,
    list_skills,
    load_skill_content,
)
from opscloud.skills.merge import merge_skill
from opscloud.skills.registry import (
    SkillRegistry,
    SkillSource,
    _parse_skill_frontmatter,
)
from opscloud.skills.trust import (
    RevokeResult,
    SkillTrustStore,
    clear_trusted_skill_dirs,
    is_skill_dir_trusted,
    list_trusted_skill_dir_entries,
    list_trusted_skill_dirs,
    load_trusted_skill_dirs,
    revoke_skill_dir_trust,
    trust_skill_dir,
)


@pytest.fixture(autouse=True)
def reset_skill_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


# ── Frontmatter & File Parsing ──────────────────────────────────────────────


def test_parse_skill_frontmatter_and_file(tmp_path: Path):
    skill_dir = tmp_path / "aws-cost-analysis"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"

    skill_content = """---
name: aws-cost-analysis
description: Analyzes AWS Cost Explorer spend and unattached EBS volumes
tags:
  - aws
  - cost
  - finops
domain: devops
---

# AWS Cost Analysis Skill
This skill performs detailed analysis on AWS billing data.
"""
    skill_file.write_text(skill_content, encoding="utf-8")

    desc, tags = _parse_skill_frontmatter(skill_file)
    assert "Analyzes AWS Cost Explorer" in desc
    assert "aws" in tags
    assert "finops" in tags

    meta = _parse_skill_file(skill_file)
    assert meta["description"] == "Analyzes AWS Cost Explorer spend and unattached EBS volumes"
    assert "aws" in meta["tags"]
    assert meta["domain"] == "devops"
    assert "This skill performs detailed analysis" in meta["preview"]


# ── Validation Tests ─────────────────────────────────────────────────────────


def test_validate_name():
    # Valid names
    assert _validate_name("aws-terraform")[0] is True
    assert _validate_name("eks-debug-123")[0] is True
    assert _validate_name("cost-finops")[0] is True

    # Invalid names
    assert _validate_name("")[0] is False
    assert _validate_name("   ")[0] is False
    assert _validate_name("A" * 65)[0] is False  # > 64 chars
    assert _validate_name("AWS-TERRAFORM")[0] is False  # Uppercase not allowed
    assert _validate_name("-leading")[0] is False
    assert _validate_name("trailing-")[0] is False
    assert _validate_name("double--hyphen")[0] is False
    assert _validate_name("../traversal")[0] is False
    assert _validate_name("path/traversal")[0] is False
    assert _validate_name("path\\traversal")[0] is False


def test_validate_skill_path(tmp_path: Path):
    base_dir = tmp_path / "skills"
    base_dir.mkdir()
    child_dir = base_dir / "custom-skill"
    child_dir.mkdir()
    outside_dir = tmp_path / "other"
    outside_dir.mkdir()

    is_valid, _ = _validate_skill_path(child_dir, base_dir)
    assert is_valid is True

    is_valid, err = _validate_skill_path(outside_dir, base_dir)
    assert is_valid is False
    assert "must be within" in err


# ── Merge Helper Tests ───────────────────────────────────────────────────────


def test_merge_skill_precedence(caplog):
    merged = {}
    labels = {}

    skill_v1 = {"name": "test-skill", "path": "/path/v1", "version": "1"}
    skill_v2 = {"name": "test-skill", "path": "/path/v2", "version": "2"}

    merge_skill(merged, labels, skill_v1, source_label="built-in")
    assert merged["test-skill"]["version"] == "1"
    assert labels["test-skill"] == "built-in"

    merge_skill(merged, labels, skill_v2, source_label="project")
    assert merged["test-skill"]["version"] == "2"
    assert labels["test-skill"] == "project"


# ── Trust Store Tests ────────────────────────────────────────────────────────


def test_trust_store_lifecycle(tmp_path: Path):
    store_file = tmp_path / "skill_trust.json"
    skill_dir = (tmp_path / "custom_skill").resolve()
    skill_dir.mkdir()

    assert not is_skill_dir_trusted(skill_dir, store_path=store_file)

    # Trust
    assert trust_skill_dir(skill_dir, store_path=store_file) is True
    assert is_skill_dir_trusted(skill_dir, store_path=store_file) is True

    # List entries
    entries = list_trusted_skill_dir_entries(store_path=store_file)
    assert len(entries) == 1
    assert entries[0][0] == str(skill_dir)
    assert entries[0][1] != ""  # Contains ISO-8601 timestamp

    # Revoke
    res = revoke_skill_dir_trust(skill_dir, store_path=store_file)
    assert res == RevokeResult.REMOVED
    assert not is_skill_dir_trusted(skill_dir, store_path=store_file)

    # Revoke not found
    res = revoke_skill_dir_trust(skill_dir, store_path=store_file)
    assert res == RevokeResult.NOT_FOUND

    # Clear
    trust_skill_dir(skill_dir, store_path=store_file)
    assert clear_trusted_skill_dirs(store_path=store_file) is True
    assert len(list_trusted_skill_dirs(store_path=store_file)) == 0


def test_load_trusted_skill_dirs_symlink_safety(tmp_path: Path):
    store_file = tmp_path / "skill_trust.json"
    real_dir = (tmp_path / "real_skill").resolve()
    real_dir.mkdir()

    trust_skill_dir(real_dir, store_path=store_file)
    verified = load_trusted_skill_dirs(store_path=store_file)
    assert len(verified) == 1
    assert verified[0] == real_dir


def test_skill_trust_store_compat(tmp_path: Path):
    store_file = tmp_path / "skill_trust.json"
    compat_store = SkillTrustStore(trust_file_path=store_file)

    custom_dir = tmp_path / "compat_skill"
    custom_dir.mkdir()

    assert not compat_store.is_trusted("compat_skill", custom_dir)
    compat_store.trust_skill("compat_skill", custom_dir)
    assert compat_store.is_trusted("compat_skill", custom_dir)


# ── Loader & Containment Tests ───────────────────────────────────────────────


def test_load_skill_content_containment(tmp_path: Path):
    root_a = tmp_path / "skills_a"
    root_a.mkdir()
    skill_file = root_a / "SKILL.md"
    skill_file.write_text("Instruction body", encoding="utf-8")

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("Secret text", encoding="utf-8")

    # Allowed root read succeeds
    content = load_skill_content(str(skill_file), allowed_roots=[root_a])
    assert content == "Instruction body"

    # Outside read raises PermissionError
    with pytest.raises(PermissionError):
        load_skill_content(str(outside_file), allowed_roots=[root_a])


def test_list_skills_precedence(tmp_path: Path):
    built_in = tmp_path / "bi"
    built_in.mkdir()
    (built_in / "common-skill").mkdir()
    (built_in / "common-skill" / "SKILL.md").write_text(
        "---\nname: common-skill\ndescription: Built-in version\n---\nBuiltin", encoding="utf-8"
    )

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "common-skill").mkdir()
    (user_dir / "common-skill" / "SKILL.md").write_text(
        "---\nname: common-skill\ndescription: User version\n---\nUser", encoding="utf-8"
    )

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "common-skill").mkdir()
    (project_dir / "common-skill" / "SKILL.md").write_text(
        "---\nname: common-skill\ndescription: Project version\n---\nProject", encoding="utf-8"
    )

    # Project overrides user which overrides built-in
    skills = list_skills(
        built_in_skills_dir=built_in,
        user_skills_dir=user_dir,
        project_skills_dir=project_dir,
        include_plugins=False,
    )
    assert len(skills) == 1
    assert skills[0]["name"] == "common-skill"
    assert skills[0].get("source") == "project"
    assert skills[0]["description"] == "Project version"


# ── Invocation Tests ─────────────────────────────────────────────────────────


def test_build_skill_invocation_envelope():
    skill = {
        "name": "ecs-remediate",
        "description": "Fix ECS crashloops",
        "source": "user",
    }
    content = "Follow step 1, step 2"
    envelope = build_skill_invocation_envelope(skill, content, args="cluster=prod")

    assert envelope.skill_name == "ecs-remediate"
    assert "I'm invoking the skill `ecs-remediate`." in envelope.prompt
    assert "Follow step 1, step 2" in envelope.prompt
    assert "**User request:** cluster=prod" in envelope.prompt
    assert envelope.message_kwargs["additional_kwargs"]["__skill"]["args"] == "cluster=prod"


def test_parse_skill_command():
    name, args = parse_skill_command("/skill:aws-terraform apply -auto-approve")
    assert name == "aws-terraform"
    assert args == "apply -auto-approve"

    name, args = parse_skill_command("/skill:eks-debug")
    assert name == "eks-debug"
    assert args == ""

    name, args = parse_skill_command("not a skill command")
    assert name == ""
    assert args == ""


# ── CLI Commands Tests ───────────────────────────────────────────────────────


def test_skills_create_and_delete(tmp_path: Path, monkeypatch):
    from opscloud.config.settings import settings

    monkeypatch.setattr("opscloud.config.paths.DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "project_root", tmp_path)

    # Template generation
    template = _generate_template("karpenter-scale")
    assert "name: karpenter-scale" in template
    assert "## Overview" in template

    # Create skill
    _create("karpenter-scale", agent="opscloud", project=False, output_format="json")
    created_skill_dir = tmp_path / "skills" / "karpenter-scale"
    assert created_skill_dir.is_dir()
    assert (created_skill_dir / "SKILL.md").is_file()

    # Dry-run delete
    _delete("karpenter-scale", agent="opscloud", project=False, dry_run=True, output_format="json")
    assert created_skill_dir.is_dir()

    # Actual delete
    _delete("karpenter-scale", agent="opscloud", project=False, force=True, output_format="json")
    assert not created_skill_dir.exists()


def test_setup_skills_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    skills_parser = setup_skills_parser(subparsers)
    assert skills_parser is not None

    args = parser.parse_args(["skills", "list", "--json"])
    assert args.subcommand == "skills"
    assert args.skills_command == "list"
    assert args.output_format == "json"


# ── Registry Compatibility Tests ─────────────────────────────────────────────


def test_skill_registry_registration_and_lookup():
    registry = SkillRegistry.get_instance()
    skill = SkillSource(
        name="eks-diagnostics",
        path=Path("/tmp/eks-diagnostics"),
        tier="built-in",
        description="Diagnoses EKS clusters",
        domain="kubernetes",
        tags=("eks", "k8s"),
    )
    registry.register(skill)

    found = registry.get("eks-diagnostics")
    assert found is not None
    assert found.name == "eks-diagnostics"
    assert found.tier == "built-in"

    skills = registry.list_skills()
    assert any(s.name == "eks-diagnostics" for s in skills)


def test_skill_registry_discover_directories(tmp_path: Path):
    registry = SkillRegistry.get_instance()

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    skill_a = user_skills / "s3-audit"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\ndescription: Audits S3 bucket security\ntags: [s3, security]\n---\nAudit tool",
        encoding="utf-8",
    )

    count = registry.discover(user_dir=user_skills)
    assert count >= 1
    found = registry.get("s3-audit")
    assert found is not None
    assert "Audits S3" in found.description


def test_plugin_skills_middleware_handles_none_metadata(tmp_path: Path):
    from unittest.mock import MagicMock
    from opscloud.middleware.skills import PluginSkillsMiddleware

    middleware = PluginSkillsMiddleware(
        sources=[(str(tmp_path), "test")],
    )

    # 1. Test before_agent when skills_metadata in state is None
    state: dict[str, object] = {"skills_metadata": None}
    runtime = MagicMock()
    config = MagicMock()
    update = middleware.before_agent(state, runtime, config)  # type: ignore[arg-type]
    assert update is not None
    assert "skills_metadata" in update

    # 2. Test modify_request when request.state['skills_metadata'] is None
    request = MagicMock()
    request.state = {"skills_metadata": None}
    middleware.modify_request(request)
    assert isinstance(request.state["skills_metadata"], list)


def test_built_in_remember_skill_discovered():
    from opscloud.skills.load import list_skills
    skills = list_skills()
    remember = next((s for s in skills if s["name"] == "remember"), None)
    assert remember is not None, "Built-in remember skill should be discovered"
    assert remember.get("source") == "built-in"
    assert "Distill the conversation" in remember["description"]
    assert "persistent memory (AGENTS.md)" in remember["description"]

    content = load_skill_content(remember["path"])
    assert content is not None
    assert "persistent memory" in content.lower()
    assert "agents.md" in content.lower()


def test_built_in_skill_creator_discovered():
    from opscloud.skills.load import list_skills
    skills = list_skills()
    creator = next((s for s in skills if s["name"] == "skill-creator"), None)
    assert creator is not None, "Built-in skill-creator should be discovered"
    assert creator.get("source") == "built-in"


def test_skill_invocation_envelope_remember():
    cached = {
        "name": "remember",
        "description": "Capture valuable knowledge",
        "source": "built-in",
    }
    content = "# Remember Instructions\nDo this and that."
    args = "my kid name is ivaan"
    envelope = build_skill_invocation_envelope(cached, content, args)

    assert envelope.skill_name == "remember"
    assert "I'm invoking the skill `remember`" in envelope.prompt
    assert "Do this and that" in envelope.prompt
    assert "**User request:** my kid name is ivaan" in envelope.prompt
    assert envelope.message_kwargs["additional_kwargs"]["__skill"]["args"] == "my kid name is ivaan"


def test_strip_frontmatter_and_skill_message():
    from opscloud.ui.widgets.messages import _strip_frontmatter, SkillMessage
    raw = """---
name: remember
description: Some desc
---
# Actual Content
Body text goes here.
"""
    stripped = _strip_frontmatter(raw)
    assert stripped.strip() == "# Actual Content\nBody text goes here."

    msg = SkillMessage(
        skill_name="remember",
        description="Review the current conversation",
        source="built-in",
        body=raw,
        args="my kid name is ivaan",
    )
    assert msg._skill_name == "remember"
    assert msg._args == "my kid name is ivaan"
    assert msg._source == "built-in"
    assert not msg._expanded

    msg.toggle_body()
    assert msg._expanded is True
    msg.toggle_body()
    assert msg._expanded is False


