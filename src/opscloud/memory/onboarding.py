"""First-run interactive onboarding wizard and memory preference management for opscloud."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

from opscloud.config.paths import DATA_DIR, ensure_agent_dir, user_agent_md
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

ONBOARDING_NAME_MEMORY_START = "<!-- opscloud:onboarding-name:start -->"
ONBOARDING_NAME_MEMORY_END = "<!-- opscloud:onboarding-name:end -->"


def _normalize_memory_name(name: str) -> str:
    return " ".join(name.split())


def _onboarding_name_memory_block(
    name: str = "Cloud Platform & DevOps Engineer",
    provider: str = "custom-model",
    cloud: str = "AWS",
    region: str = "us-east-1",
    iac: str = "Terraform",
) -> str:
    """Format the machine-managed onboarding block with comment markers."""
    return (
        f"{ONBOARDING_NAME_MEMORY_START}\n"
        f"- The user's role/name is \"{name}\".\n"
        f"- Primary Cloud Provider: {cloud}\n"
        f"- Default Region: {region}\n"
        f"- IaC Tool Preference: {iac}\n"
        f"- Primary Model Provider: {provider}\n"
        f"{ONBOARDING_NAME_MEMORY_END}"
    )


def _upsert_onboarding_name_memory(existing: str, block: str) -> str:
    """Insert or replace the machine-managed onboarding block."""
    start = existing.find(ONBOARDING_NAME_MEMORY_START)
    end = existing.find(ONBOARDING_NAME_MEMORY_END)
    if start != -1 and end != -1 and start < end:
        end += len(ONBOARDING_NAME_MEMORY_END)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].strip()
        parts = [part for part in (prefix, block, suffix) if part]
        return "\n\n".join(parts).rstrip() + "\n"

    base = existing.rstrip()
    if not base:
        return f"## User Preferences & Defaults\n\n{block}\n"
    if "## User Preferences" in base or "## User Preferences & Defaults" in base:
        return f"{base}\n\n{block}\n"
    return f"{base}\n\n## User Preferences & Defaults\n\n{block}\n"


def extract_onboarding_name_block(text: str) -> str | None:
    """Extract the managed onboarding block (including markers) if present."""
    start = text.find(ONBOARDING_NAME_MEMORY_START)
    end = text.find(ONBOARDING_NAME_MEMORY_END)
    if start == -1 or end == -1 or start >= end:
        return None
    return text[start : end + len(ONBOARDING_NAME_MEMORY_END)]


def strip_onboarding_name_markers(text: str) -> str:
    """Remove onboarding markers from text while preserving inner content."""
    return text.replace(ONBOARDING_NAME_MEMORY_START, "").replace(
        ONBOARDING_NAME_MEMORY_END, ""
    )


def run_onboarding_if_needed(agent_name: str = "opscloud") -> None:
    """Run onboarding on interactive terminal if user AGENTS.md is missing or empty."""
    user_md = user_agent_md(agent_name)
    if user_md.is_file() and user_md.stat().st_size > 0:
        return

    # Check if standard input is interactive
    if not sys.stdin.isatty():
        logger.info("Non-interactive terminal detected; seeding default cloud preferences.")
        block = _onboarding_name_memory_block()
        try:
            ensure_agent_dir(agent_name)
            user_md.write_text(f"## User Preferences & Defaults\n\n{block}\n", encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write default onboarding memory file: %s", e)
        return

    print("====================================================")
    print("      Welcome to OpsCloud Setup & Preferences!      ")
    print("====================================================")
    try:
        name = input("Enter your preferred name or role (default: Cloud Platform & DevOps Engineer): ").strip() or "Cloud Platform & DevOps Engineer"
        cloud = input("Primary Cloud Provider (AWS, GCP, Azure, Multi, default: AWS): ").strip() or "AWS"
        region = input("Default Region (e.g. us-east-1, us-west-2, default: us-east-1): ").strip() or "us-east-1"
        iac = input("IaC Tool Preference (Terraform, OpenTofu, Helm, None, default: Terraform): ").strip() or "Terraform"
        provider = input("LLM Model / Provider (default: anthropic): ").strip() or "anthropic"
    except (KeyboardInterrupt, EOFError):
        print("\nOnboarding aborted. Using default configuration.")
        name, cloud, region, iac, provider = (
            "Cloud Platform & DevOps Engineer",
            "AWS",
            "us-east-1",
            "Terraform",
            "anthropic",
        )

    block = _onboarding_name_memory_block(
        name=name, provider=provider, cloud=cloud, region=region, iac=iac
    )
    try:
        ensure_agent_dir(agent_name)
        user_md.write_text(f"## User Preferences & Defaults\n\n{block}\n", encoding="utf-8")
        print(f"Setup complete! Preferences saved to {user_md}")
    except OSError as e:
        logger.error("Failed to write onboarding memory file: %s", e)
