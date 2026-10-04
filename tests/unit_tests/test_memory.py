"""Unit tests for OpsCloud memory registry, branch memory store, and guard middleware."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.store.memory import InMemoryStore

from opscloud.memory.branch import BranchMemoryStore
from opscloud.memory.guard import ManagedMemoryGuardMiddleware
from opscloud.memory.registry import MemoryRegistry


def test_branch_memory_store(tmp_path: Path):
    store = BranchMemoryStore("eks-diagnostics", run_id="run-1234", project_root=tmp_path)
    assert store.subagent_name == "eks-diagnostics"
    assert store.run_id == "run-1234"

    # Write observation
    file_path = store.write_observation("Node ip-10-0-1-20 is NotReady due to DiskPressure.")
    assert file_path.exists()
    content = store.get_content()
    assert "DiskPressure" in content
    assert "eks-diagnostics" in content


def test_memory_registry_scopes(tmp_path: Path):
    registry = MemoryRegistry.get_instance()
    paths = registry.get_memory_paths_for_scope("auto", project_root=tmp_path)
    assert len(paths) >= 1

    sources = registry.get_all_memory_sources(project_root=tmp_path)
    assert isinstance(sources, list)
    assert len(sources) >= 1


def test_managed_memory_guard(tmp_path: Path):
    guarded_file = tmp_path / "AGENTS.md"
    guarded_file.touch()

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[guarded_file])

    # Attempt to write to guarded file
    bad_req = SimpleNamespace(
        tool_call={"name": "write_file", "args": {"file_path": str(guarded_file)}}
    )
    assert guard._guarded_path(bad_req) == guarded_file.resolve()

    # Attempt to write to non-guarded file
    safe_req = SimpleNamespace(
        tool_call={"name": "write_file", "args": {"file_path": str(tmp_path / "normal.txt")}}
    )
    assert guard._guarded_path(safe_req) is None


@pytest.mark.asyncio
async def test_memory_registry_store_operations() -> None:
    """Verify thread-isolated memory save, get, and list operations."""
    store = InMemoryStore()
    registry = MemoryRegistry(store=store)

    await registry.save_memory("thread-1", "user_preference", "always use us-east-1")
    val = await registry.get_memory("thread-1", "user_preference")
    assert val == "always use us-east-1"

    # Isolation check: thread-2 should not see thread-1's memory
    val2 = await registry.get_memory("thread-2", "user_preference")
    assert val2 is None

    # List memories
    items = await registry.list_memories("thread-1")
    assert len(items) == 1
    assert items[0]["key"] == "user_preference"
    assert items[0]["content"] == "always use us-east-1"


def test_memory_guard_middleware(tmp_path: Path) -> None:
    """Verify ManagedMemoryGuardMiddleware protects managed blocks and deletion while allowing user edits."""
    managed_block = (
        "<!-- opscloud:onboarding-name:start -->\n"
        "- The user's preferred name is \"Alex\".\n"
        "<!-- opscloud:onboarding-name:end -->\n"
    )
    initial_content = f"# Protected Memory\n\n{managed_block}\n- Primary region: us-west-2\n"
    guarded_file = tmp_path / "AGENTS.md"
    guarded_file.write_text(initial_content, encoding="utf-8")

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[guarded_file])

    # 1. Attempt to delete guarded file -> rejected outright
    req_delete = MagicMock()
    req_delete.tool_call = {
        "id": "tc-del",
        "name": "delete_file",
        "args": {"file_path": str(guarded_file)},
    }
    del_handler = MagicMock()
    del_res = guard.wrap_tool_call(req_delete, del_handler)
    assert isinstance(del_res, ToolMessage)
    assert del_res.status == "error"
    assert "must not be deleted" in del_res.content
    del_handler.assert_not_called()

    # 2. Edit that leaves managed block intact (adding user memory) -> allowed to proceed!
    req_add_memory = MagicMock()
    req_add_memory.tool_call = {
        "id": "tc-edit",
        "name": "edit_file",
        "args": {
            "file_path": str(guarded_file),
            "old_string": "- Primary region: us-west-2",
            "new_string": "- Primary region: us-west-2\n- Secondary region: us-east-1",
        },
    }

    def simulate_add_memory(req: Any) -> ToolMessage:
        content = guarded_file.read_text(encoding="utf-8")
        updated = content.replace(
            "- Primary region: us-west-2",
            "- Primary region: us-west-2\n- Secondary region: us-east-1",
        )
        guarded_file.write_text(updated, encoding="utf-8")
        return ToolMessage(content="File edited successfully", tool_call_id=req.tool_call["id"])

    edit_res = guard.wrap_tool_call(req_add_memory, simulate_add_memory)
    assert isinstance(edit_res, ToolMessage)
    assert edit_res.content == "File edited successfully"
    # Verify the edit succeeded on disk
    on_disk = guarded_file.read_text(encoding="utf-8")
    assert "Secondary region: us-east-1" in on_disk
    assert "The user's preferred name is \"Alex\"." in on_disk

    # 3. Edit that corrupts/deletes the managed block -> managed block is restored & error returned
    req_clobber = MagicMock()
    req_clobber.tool_call = {
        "id": "tc-clobber",
        "name": "write_file",
        "args": {"file_path": str(guarded_file), "content": "completely replaced memory"},
    }

    def simulate_clobber(req: Any) -> ToolMessage:
        guarded_file.write_text("completely replaced memory", encoding="utf-8")
        return ToolMessage(content="Wrote 25 bytes", tool_call_id=req.tool_call["id"])

    clobber_res = guard.wrap_tool_call(req_clobber, simulate_clobber)
    assert isinstance(clobber_res, ToolMessage)
    assert clobber_res.status == "error"
    assert "must not be edited" in clobber_res.content
    # Verify managed block was restored on disk
    restored_on_disk = guarded_file.read_text(encoding="utf-8")
    assert "The user's preferred name is \"Alex\"." in restored_on_disk

    # 4. Edit non-protected file -> allowed directly
    normal_file = tmp_path / "deployment.yaml"
    req_allowed = MagicMock()
    req_allowed.tool_call = {
        "id": "tc-normal",
        "name": "write_file",
        "args": {"file_path": str(normal_file), "content": "kind: Deployment"},
    }
    normal_handler = MagicMock()
    normal_handler.return_value = ToolMessage(content="Wrote 20 bytes", tool_call_id="tc-normal")

    result = guard.wrap_tool_call(req_allowed, normal_handler)
    assert isinstance(result, ToolMessage)
    assert result.content == "Wrote 20 bytes"
    normal_handler.assert_called_once_with(req_allowed)


def test_ensure_user_agent_md_and_discovery(tmp_path: Path, monkeypatch) -> None:
    """Verify that ~/.opscloud/AGENTS.md is automatically created and discovered."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    from opscloud.config import paths as app_paths

    # Re-evaluate DATA_DIR with fake_home
    monkeypatch.setattr(app_paths, "DATA_DIR", fake_home / ".opscloud")

    # Initial state: fake_home does not exist
    primary_file = app_paths.ensure_user_agent_md()
    assert primary_file.exists()
    assert primary_file == fake_home / ".opscloud" / "AGENTS.md"

    # Verify discovery in MemoryRegistry
    registry = MemoryRegistry.get_instance()
    sources = registry.get_all_memory_sources(project_root=tmp_path / "proj")
    assert str(primary_file.resolve()) in sources
    # Verify .agents is not in user memory sources
    assert not any(".agents" in s for s in sources if str(fake_home) in s)


def test_memory_guard_extended_tools_and_target_file(tmp_path: Path) -> None:
    """Verify memory guard works with TargetFile arg and write_to_file / delete aliases."""
    guarded_file = tmp_path / "AGENTS.md"
    guarded_file.write_text("## Initial Memory\n", encoding="utf-8")

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[guarded_file])

    # delete using rm alias and target arg
    req_rm = MagicMock()
    req_rm.tool_call = {
        "id": "tc-rm",
        "name": "rm",
        "args": {"target": str(guarded_file)},
    }
    rm_handler = MagicMock()
    res_rm = guard.wrap_tool_call(req_rm, rm_handler)
    assert isinstance(res_rm, ToolMessage)
    assert res_rm.status == "error"
    assert "must not be deleted" in res_rm.content
    rm_handler.assert_not_called()

    # write using write_to_file and TargetFile arg
    req_write = MagicMock()
    req_write.tool_call = {
        "id": "tc-write",
        "name": "write_to_file",
        "args": {"TargetFile": str(guarded_file), "CodeContent": "new content"},
    }
    write_handler = MagicMock()
    write_handler.return_value = ToolMessage(content="Written", tool_call_id="tc-write")
    res_write = guard.wrap_tool_call(req_write, write_handler)
    assert isinstance(res_write, ToolMessage)
    write_handler.assert_called_once()
