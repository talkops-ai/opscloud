"""Integration tests for ServerConfig and langgraph.json generation."""

from pathlib import Path
from opscloud.server import ServerConfig, generate_langgraph_json


def test_server_config_roundtrip():
    cfg = ServerConfig(
        model="anthropic:claude-3-5-sonnet-latest",
        interactive=True,
        approval_mode="auto",
    )
    env = cfg.to_env()
    assert "OPSCLOUD_SERVER_INTERACTIVE" in env
    assert env["OPSCLOUD_SERVER_INTERACTIVE"] == "true"


def test_generate_langgraph_json(tmp_path: Path):
    target = generate_langgraph_json(tmp_path)
    assert target.exists()
    assert target.name == "langgraph.json"
    content = target.read_text(encoding="utf-8")
    assert "server_graph.py:make_graph" in content
