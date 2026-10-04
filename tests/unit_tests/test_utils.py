"""Unit tests for OpsCloud utils (git metadata, cost estimation, session stats)."""

from pathlib import Path
import pytest
from opscloud.utils.git import (
    parse_repository_metadata,
    find_git_root,
    resolve_git_branch,
    RepositoryMetadata,
)
from opscloud.utils.cost_estimation import TokenCostEstimator
from opscloud.utils.session_stats import SessionStats


def test_parse_repository_metadata():
    # SSH format
    meta = parse_repository_metadata("git@github.com:talkops/opscloud.git")
    assert meta is not None
    assert meta.provider == "github"
    assert meta.name == "talkops/opscloud"
    assert meta.url == "https://github.com/talkops/opscloud"

    # HTTPS format
    meta_https = parse_repository_metadata("https://gitlab.com/group/subproject.git")
    assert meta_https is not None
    assert meta_https.provider == "gitlab"
    assert meta_https.name == "group/subproject"
    assert meta_https.url == "https://gitlab.com/group/subproject"

    # Invalid
    assert parse_repository_metadata("") is None
    assert parse_repository_metadata("not-a-valid-url") is None


def test_git_root_and_branch_in_current_repo():
    root = find_git_root(Path.cwd())
    assert root is not None
    assert root.exists()

    branch = resolve_git_branch(Path.cwd())
    assert branch is not None
    assert len(branch) > 0


def test_session_stats():
    stats = SessionStats()
    stats.record_request("gpt-4o", input_toks=100, output_toks=50, provider="openai", cost_usd=0.005)
    assert stats.request_count == 1
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.total_cost_usd == 0.005


def test_token_cost_estimator():
    assert isinstance(TokenCostEstimator.is_available(), bool)
