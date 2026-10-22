from app.agent.tools import (
    ToolContext,
    browse_tool,
    download_file_tool,
    edit_file_tool,
    http_get_tool,
    read_file_tool,
    run_shell_tool,
    write_file_tool,
)
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime


class _FakePage:
    def __init__(self):
        self.url = None

    def goto(self, url):
        self.url = url

    def title(self):
        return "Example Title"

    def inner_text(self, selector):
        return "Example visible text"


class _FakeResponse:
    def body(self):
        return b"binary-file-bytes"


class _FakeRequestAPI:
    def get(self, url):
        return _FakeResponse()


class _FakeBrowserContext:
    def __init__(self):
        self.request = _FakeRequestAPI()

    def new_page(self):
        return _FakePage()


class _FakeBrowserService:
    def __init__(self):
        self.context = _FakeBrowserContext()

    def get_or_create_context(self, task_id):
        return self.context


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


def test_browse_tool_returns_title_and_text():
    ctx = ToolContext(task_id=1, session=None, browser_service=_FakeBrowserService())
    result = browse_tool.run({"url": "http://example.com"}, ctx)
    assert result["ok"] is True
    assert "Example Title" in result["output"]
    assert "Example visible text" in result["output"]


def test_browse_tool_without_browser_service_returns_not_available():
    ctx = ToolContext(task_id=1, session=None)
    result = browse_tool.run({"url": "http://example.com"}, ctx)
    assert result["ok"] is False


def test_download_file_tool_writes_decoded_bytes_via_sandbox(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(
        task_id=1,
        session=None,
        sandbox_manager=manager,
        browser_service=_FakeBrowserService(),
    )
    result = download_file_tool.run({"url": "http://example.com/file.bin", "save_as": "out.bin"}, ctx)
    assert result["ok"] is True

    read_result = manager.read_file(1, "out.bin")
    assert "binary-file-bytes" in read_result["output"]


def test_download_file_tool_without_browser_service_returns_not_available(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    result = download_file_tool.run({"url": "http://example.com/file.bin", "save_as": "out.bin"}, ctx)
    assert result["ok"] is False
