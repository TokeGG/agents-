from app.agent.tools import (
    ToolContext,
    edit_file_tool,
    http_get_tool,
    read_file_tool,
    run_shell_tool,
    write_file_tool,
)
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime


def test_run_shell_tool_executes_via_sandbox_manager(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    result = run_shell_tool.run({"command": "echo hi"}, ctx)
    assert result["ok"] is True
    assert "hi" in result["output"]


def test_write_then_read_file_tool_round_trips(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    write_result = write_file_tool.run({"path": "out.txt", "content": "hello world"}, ctx)
    assert write_result["ok"] is True
    read_result = read_file_tool.run({"path": "out.txt"}, ctx)
    assert read_result["ok"] is True
    assert "hello world" in read_result["output"]


def test_edit_file_tool_replaces_text(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    write_file_tool.run({"path": "out.txt", "content": "hello world"}, ctx)
    edit_result = edit_file_tool.run({"path": "out.txt", "old": "world", "new": "there"}, ctx)
    assert edit_result["ok"] is True
    read_result = read_file_tool.run({"path": "out.txt"}, ctx)
    assert "hello there" in read_result["output"]


def test_http_get_routes_through_sandbox_when_manager_present(tmp_path, monkeypatch):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    calls = []
    monkeypatch.setattr(
        manager,
        "run_shell",
        lambda task_id, command, timeout=120: calls.append(command)
        or {"ok": True, "output": "status=200", "data": None},
    )
    result = http_get_tool.run({"url": "http://example.com"}, ctx)
    assert result["ok"] is True
    assert calls  # routed through run_shell, not httpx.get directly
