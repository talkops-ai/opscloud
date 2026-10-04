"""Compatibility shim for opscloud.skills.loader -> opscloud.skills.load."""

from __future__ import annotations

from opscloud.skills.load import (
    ExtendedSkillMetadata,
    SkillMetadata,
    _parse_skill_file,
    get_skill_by_name,
    get_skill_content_by_name,
    list_skills,
    load_skill_content,
)

__all__ = [
    "ExtendedSkillMetadata",
    "SkillMetadata",
    "_parse_skill_file",
    "get_skill_by_name",
    "get_skill_content_by_name",
    "list_skills",
    "load_skill_content",
]
