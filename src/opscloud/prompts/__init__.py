"""OpsCloud prompts module and dynamic cloud-native system prompt builder."""

from __future__ import annotations

from opscloud.prompts.system import (
    MODEL_IDENTITY_RE,
    OPSCLOUD_MEMORY_HEADLESS_SYSTEM_PROMPT,
    OPSCLOUD_MEMORY_READONLY_SYSTEM_PROMPT,
    OPSCLOUD_MEMORY_SYSTEM_PROMPT,
    build_cloud_provider_section,
    build_fs_tool_guidance,
    build_model_identity_section,
    build_working_dir_section,
    get_base_system_prompt,
    get_memory_system_prompt,
)
from opscloud.prompts.types import (
    CloudProvider,
    FsToolName,
    normalize_cloud_provider,
)

__all__ = [
    "CloudProvider",
    "FsToolName",
    "MODEL_IDENTITY_RE",
    "OPSCLOUD_MEMORY_HEADLESS_SYSTEM_PROMPT",
    "OPSCLOUD_MEMORY_READONLY_SYSTEM_PROMPT",
    "OPSCLOUD_MEMORY_SYSTEM_PROMPT",
    "build_cloud_provider_section",
    "build_fs_tool_guidance",
    "build_model_identity_section",
    "build_working_dir_section",
    "get_base_system_prompt",
    "get_memory_system_prompt",
    "normalize_cloud_provider",
]
