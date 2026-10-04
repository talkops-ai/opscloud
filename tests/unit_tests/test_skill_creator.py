"""Unit tests for the skill-creator built-in skill, scripts, and slash command integration."""

import importlib.util
import os
from pathlib import Path
import pytest

from opscloud.commands import CommandContext, CommandRouter
from opscloud.commands.power.skill_creator import SkillCreatorHandler
from opscloud.ui.command_registry import (
    COMMANDS,
    _STATIC_SKILL_ALIASES,
    build_skill_commands,
    get_all_entries,
    get_command,
)


def _load_script_module(script_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_creator_built_in_files_exist():
    """Verify built-in skill-creator has SKILL.md and executable helper scripts."""
    root_skill_dir = (
        Path(__file__).parent.parent.parent
        / "src"
        / "opscloud"
        / "built_in_skills"
        / "skill-creator"
    )
    assert root_skill_dir.is_dir()

    skill_md = root_skill_dir / "SKILL.md"
    assert skill_md.is_file()

    init_script = root_skill_dir / "scripts" / "init_skill.py"
    assert init_script.is_file()
    assert os.access(init_script, os.X_OK)

    validate_script = root_skill_dir / "scripts" / "quick_validate.py"
    assert validate_script.is_file()
    assert os.access(validate_script, os.X_OK)


def test_quick_validate_on_skill_creator():
    """Verify that quick_validate.py validates the skill-creator skill itself."""
    root_skill_dir = (
        Path(__file__).parent.parent.parent
        / "src"
        / "opscloud"
        / "built_in_skills"
        / "skill-creator"
    )
    validate_script = root_skill_dir / "scripts" / "quick_validate.py"
    val_mod = _load_script_module(validate_script, "quick_validate")

    is_valid, msg = val_mod.validate_skill(root_skill_dir)
    assert is_valid is True, f"Validation failed: {msg}"
    assert "Skill is valid" in msg


def test_init_skill_and_quick_validate_lifecycle(tmp_path: Path):
    """Test creating a new skill via init_skill and validating it with quick_validate."""
    root_skill_dir = (
        Path(__file__).parent.parent.parent
        / "src"
        / "opscloud"
        / "built_in_skills"
        / "skill-creator"
    )
    init_script = root_skill_dir / "scripts" / "init_skill.py"
    validate_script = root_skill_dir / "scripts" / "quick_validate.py"

    init_mod = _load_script_module(init_script, "init_skill")
    val_mod = _load_script_module(validate_script, "quick_validate")

    # Name validation
    assert init_mod._validate_name("valid-skill-name")[0] is True
    assert init_mod._validate_name("Invalid_Name")[0] is False
    assert init_mod._validate_name("-leading-hyphen")[0] is False
    assert init_mod._validate_name("trailing-hyphen-")[0] is False
    assert init_mod._validate_name("double--hyphen")[0] is False

    # Initialize skill
    created_dir = init_mod.init_skill("demo-cloud-skill", tmp_path)
    assert created_dir is not None
    assert created_dir.is_dir()
    assert (created_dir / "SKILL.md").is_file()
    assert (created_dir / "scripts" / "example.py").is_file()
    assert (created_dir / "references" / "api_reference.md").is_file()
    assert (created_dir / "assets" / "example_asset.txt").is_file()

    # Validate initialized skill
    is_valid, msg = val_mod.validate_skill(created_dir)
    assert is_valid is True, f"Validation failed on created skill: {msg}"

    # Verify duplicate creation protection
    duplicate = init_mod.init_skill("demo-cloud-skill", tmp_path)
    assert duplicate is None


def test_quick_validate_catches_invalid_skills(tmp_path: Path):
    """Verify quick_validate identifies various invalid skill configurations."""
    root_skill_dir = (
        Path(__file__).parent.parent.parent
        / "src"
        / "opscloud"
        / "built_in_skills"
        / "skill-creator"
    )
    validate_script = root_skill_dir / "scripts" / "quick_validate.py"
    val_mod = _load_script_module(validate_script, "quick_validate")

    # 1. Missing SKILL.md
    empty_dir = tmp_path / "empty-skill"
    empty_dir.mkdir()
    valid, msg = val_mod.validate_skill(empty_dir)
    assert not valid
    assert "SKILL.md not found" in msg

    # 2. No frontmatter
    skill_dir = tmp_path / "no-frontmatter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Just Markdown\nNo frontmatter here.")
    valid, msg = val_mod.validate_skill(skill_dir)
    assert not valid
    assert "No YAML frontmatter found" in msg

    # 3. Missing description
    missing_desc_dir = tmp_path / "missing-desc"
    missing_desc_dir.mkdir()
    (missing_desc_dir / "SKILL.md").write_text("---\nname: missing-desc\n---\n# Body")
    valid, msg = val_mod.validate_skill(missing_desc_dir)
    assert not valid
    assert "Missing 'description'" in msg

    # 4. Unexpected frontmatter keys
    unexpected_key_dir = tmp_path / "unexpected-key"
    unexpected_key_dir.mkdir()
    (unexpected_key_dir / "SKILL.md").write_text(
        "---\nname: bad-skill\ndescription: A skill\ninvalid_prop: bad\n---\n# Body"
    )
    valid, msg = val_mod.validate_skill(unexpected_key_dir)
    assert not valid
    assert "Unexpected key" in msg


def test_command_registry_includes_skill_creator():
    """Verify /skill-creator is in COMMANDS with proper aliases and metadata."""
    cmd = get_command("/skill-creator")
    assert cmd is not None
    assert cmd.name == "/skill-creator"
    assert "/skill:skill-creator" in cmd.aliases
    assert "Create or refine agent skills" in cmd.description


def test_command_router_dispatches_skill_creator():
    """Verify CommandRouter recognizes /skill-creator."""
    router = CommandRouter()
    router.auto_discover()

    handler = router.get_handler("/skill-creator")
    assert handler is not None
    assert isinstance(handler, SkillCreatorHandler)


@pytest.mark.asyncio
async def test_skill_creator_handler_delegation():
    """Verify SkillCreatorHandler rewrites to /skill:skill-creator."""
    handler = SkillCreatorHandler()

    class MockApp:
        def __init__(self):
            self.invoked_command = None

        async def _handle_skill_command(self, command: str) -> None:
            self.invoked_command = command

    mock_app = MockApp()
    ctx = CommandContext(
        app=mock_app,
        session=None,
        agent=None,
        settings=None,
        raw_command="/skill-creator ecs-deployer",
        args="ecs-deployer",
        thread_id="test",
        model_spec=None,
    )

    result = await handler.execute(ctx)
    assert result.success is True
    assert mock_app.invoked_command == "/skill:skill-creator ecs-deployer"


def test_build_skill_commands_autocomplete_entries():
    """Verify build_skill_commands builds /skill:<name> entries and respects _STATIC_SKILL_ALIASES."""
    mock_skills = [
        {"name": "remember", "description": "Save memories", "source": "built-in"},
        {"name": "skill-creator", "description": "Create skills", "source": "built-in"},
        {"name": "aws-eks", "description": "Manage EKS", "source": "built-in"},
        {"name": "my-plugin:audit", "description": "Plugin audit", "source": "plugin"},
    ]

    entries = build_skill_commands(mock_skills)
    names = [e.name for e in entries]

    # remember and skill-creator should be excluded because they are in _STATIC_SKILL_ALIASES
    assert "/skill:remember" not in names
    assert "/skill:skill-creator" not in names

    # Other skills should be present
    assert "/skill:aws-eks" in names
    assert "/skill:my-plugin:audit" in names

    # Plugin display name should be shortened
    plugin_entry = next(e for e in entries if e.name == "/skill:my-plugin:audit")
    assert plugin_entry.display_name == "/skill:audit"
    assert "(my-plugin)" in plugin_entry.description

    all_entries = get_all_entries(entries)
    all_names = [e.name for e in all_entries]
    assert "/skill-creator" in all_names
    assert "/skill:skill-creator" in all_names
    assert "/skill:aws-eks" in all_names
