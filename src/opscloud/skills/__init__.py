"""Skills module for OpsCloud.

Public CLI API:
- execute_skills_command: Execute skills subcommands (list/create/info/delete/trust)
- setup_skills_parser: Setup argparse configuration for skills commands

Public runtime & loader API:
- list_skills: Discover available skills across built-in, plugin, user, project, and Claude sources
- load_skill_content: Read SKILL.md content with containment validation
- build_skill_invocation_envelope: Wrap skill instructions with structured invocation metadata
- discover_skills_and_roots: Discover skills and pre-resolved containment roots
"""

from __future__ import annotations

from opscloud.skills.commands import (
    execute_skills_command,
    list_skills_command,
    setup_skills_parser,
    trust_skill_command,
)
from opscloud.skills.invocation import (
    STATIC_SKILL_ALIASES,
    SkillInvocationEnvelope,
    build_skill_invocation_envelope,
    discover_skills_and_roots,
    parse_skill_command,
)
from opscloud.skills.load import (
    ExtendedSkillMetadata,
    SkillMetadata,
    get_skill_by_name,
    get_skill_content_by_name,
    list_skills,
    load_skill_content,
)
from opscloud.skills.merge import merge_skill
from opscloud.skills.registry import (
    SkillRegistry,
    SkillSource,
    get_skill_registry,
)
from opscloud.skills.sources import (
    CodeSkillSource,
    DirectorySkillSource,
    PluginSkillSource,
    get_skill_sources,
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

__all__ = [
    "STATIC_SKILL_ALIASES",
    "ExtendedSkillMetadata",
    "RevokeResult",
    "SkillInvocationEnvelope",
    "SkillMetadata",
    "SkillRegistry",
    "SkillSource",
    "SkillTrustStore",
    "build_skill_invocation_envelope",
    "clear_trusted_skill_dirs",
    "discover_skills_and_roots",
    "execute_skills_command",
    "get_skill_by_name",
    "get_skill_content_by_name",
    "get_skill_registry",
    "get_skill_sources",
    "is_skill_dir_trusted",
    "list_skills",
    "list_skills_command",
    "list_trusted_skill_dir_entries",
    "list_trusted_skill_dirs",
    "load_skill_content",
    "load_trusted_skill_dirs",
    "merge_skill",
    "parse_skill_command",
    "revoke_skill_dir_trust",
    "setup_skills_parser",
    "trust_skill_command",
    "trust_skill_dir",
]
