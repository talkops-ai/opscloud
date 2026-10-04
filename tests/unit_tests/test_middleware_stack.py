"""Unit tests for OpsCloud middleware stack and repository bounds security."""

import pytest
from opscloud.middleware.registry import MiddlewareRegistry
from opscloud.middleware._repository_bounds import (
    RepositoryBounds,
    REPOSITORY_TOOL_NAMES,
    REPOSITORY_TOOL_CALL_LIMIT,
)
from opscloud.middleware.ask_user import AskUserMiddleware


def test_middleware_registry_discovery():
    reg = MiddlewareRegistry.ensure_discovered()
    registered = reg.list_registered()

    # Core Terminal & Cloud DevOps middlewares must be registered
    assert "ask_user" in registered
    assert "auto_mode_hitl" in registered
    assert "cost_tracking" in registered
    assert "local_context" in registered
    assert "mcp_tool" in registered
    assert "model_retry" in registered
    assert "resume_state" in registered
    assert "skills" in registered
    assert "subagents" in registered

    # Web-specific baggage must NOT be registered
    assert "a2ui_buffer" not in registered
    assert "hitl_middleware" not in registered
    assert "human_in_the_loop" not in registered
    assert "hitl_helpers" not in registered
    assert "skill_shortcut" not in registered


def test_middleware_build_stack():
    reg = MiddlewareRegistry.ensure_discovered()
    # Build stack with exclude
    stack = reg.build_stack(exclude={"ask_user", "skills", "model_retry"})
    classes = [type(m).__name__ for m in stack]
    assert "AskUserMiddleware" not in classes
    assert "CodeModelRetryMiddleware" not in classes


def test_middleware_build_single():
    reg = MiddlewareRegistry.ensure_discovered()
    mcp_mw = reg.build_middleware("mcp_tool")
    assert type(mcp_mw).__name__ == "MCPToolMiddleware"

    retry_mw = reg.build_middleware("model_retry", max_retries=5)
    assert type(retry_mw).__name__ == "CodeModelRetryMiddleware"
    assert getattr(retry_mw, "max_retries", None) == 5

    with pytest.raises(KeyError, match="not registered"):
        reg.build_middleware("non_existent_middleware")


def test_repository_bounds_safe_path():
    bounds = RepositoryBounds(backend=None, root="/workspace/project")
    assert bounds.root == "/workspace/project"

    # Valid child paths
    assert bounds.safe_path("/workspace/project/src/main.py") is True
    assert bounds.safe_path("/workspace/project/sub/dir/file.txt") is True

    # Path traversal attempts
    assert bounds.safe_path("/workspace/project/../etc/passwd") is False
    assert bounds.safe_path("/etc/passwd") is False
    assert bounds.safe_path("~/credentials") is False
    assert bounds.safe_path("relative/path") is False


def test_repository_bounds_safe_pattern():
    assert RepositoryBounds.safe_pattern("*.py") is True
    assert RepositoryBounds.safe_pattern("src/**/*.ts") is True
    assert RepositoryBounds.safe_pattern("../**/*.py") is False
    assert RepositoryBounds.safe_pattern("..") is False


def test_ask_user_middleware_instantiation():
    middleware = AskUserMiddleware()
    assert middleware is not None
