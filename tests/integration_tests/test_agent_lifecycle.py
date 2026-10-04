"""Integration tests for OpsCloud agent graph compilation."""

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from opscloud.agent.factory import create_opscloud_agent


def test_agent_graph_compilation(tmp_path):
    # Fake chat model with tool calling support capability
    model = FakeListChatModel(responses=["Hello from OpsCloud!"])
    agent, backend = create_opscloud_agent(
        model=model,
        cwd=tmp_path,
        interactive=False,
    )
    assert agent is not None
    assert backend is not None
