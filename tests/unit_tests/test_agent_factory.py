"""Unit tests for OpsCloud agent factory and LangGraph compilation."""

from pathlib import Path
from unittest.mock import MagicMock
from opscloud.agent.factory import create_opscloud_agent
from opscloud.backend.composite import OpsCloudCompositeBackend
from langchain_core.language_models.chat_models import BaseChatModel


def test_create_opscloud_agent_compilation(tmp_path):
    # Mock chat model
    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    agent_graph, backend = create_opscloud_agent(
        model=mock_model,
        cwd=tmp_path,
        interactive=False,
        auto_approve=True,
    )

    assert agent_graph is not None
    assert isinstance(backend, OpsCloudCompositeBackend)
    assert hasattr(agent_graph, "invoke")
    assert hasattr(agent_graph, "astream")


def test_create_opscloud_agent_with_project_context(tmp_path):
    from opscloud.project_utils import ProjectContext

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    user_dir = tmp_path / "custom_worktree"
    user_dir.mkdir()
    p_ctx = ProjectContext.from_user_cwd(user_dir)

    agent_graph, backend = create_opscloud_agent(
        model=mock_model,
        project_context=p_ctx,
        interactive=False,
        auto_approve=True,
    )

    assert agent_graph is not None
    # Verify the backend's default cwd matches project_context.user_cwd
    default_backend = getattr(backend, "default", None)
    assert default_backend is not None
    assert Path(default_backend.cwd).resolve() == user_dir.resolve()
