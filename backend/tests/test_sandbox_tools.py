import pytest
from playwright.sync_api import Error as PlaywrightError

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


class _FakeBrowserService:
    """Fake of the higher-level BrowserService API tools.py actually calls
    (navigate/download), rather than handing out raw Playwright objects --
    see BrowserService's module docstring for why tools no longer drive
    page/context objects directly."""

    def __init__(self, download_bytes=b"binary-file-bytes", navigate_error=None, download_error=None):
        self.download_bytes = download_bytes
        self.navigate_error = navigate_error
        self.download_error = download_error
        self.navigate_calls = []
        self.download_calls = []

    def navigate(self, task_id, url):
        self.navigate_calls.append((task_id, url))
        if self.navigate_error is not None:
            raise self.navigate_error
        return "Example Title", "Example visible text"

    def download(self, task_id, url):
        self.download_calls.append((task_id, url))
        if self.download_error is not None:
            raise self.download_error
        return self.download_bytes


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


def test_download_file_tool_round_trips_full_byte_range(tmp_path):
    """The existing ASCII-only round-trip test wouldn't catch corruption of
    non-UTF-8/NUL bytes -- exactly what base64-encoding the download exists
    to handle. Use every byte value 0-255 and verify byte-for-byte equality
    by reading the file back from the host-side temp directory as raw
    bytes, bypassing read_file's text-returning path entirely."""
    payload = bytes(range(256))
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(
        task_id=1,
        session=None,
        sandbox_manager=manager,
        browser_service=_FakeBrowserService(download_bytes=payload),
    )
    manager.get_or_create_container(1)
    result = download_file_tool.run({"url": "http://example.com/file.bin", "save_as": "out.bin"}, ctx)
    assert result["ok"] is True

    volume_name = manager._volume_name(1)
    host_path = tmp_path / volume_name / "out.bin"
    assert host_path.read_bytes() == payload


def test_browse_tool_catches_playwright_error(monkeypatch):
    ctx = ToolContext(
        task_id=1,
        session=None,
        browser_service=_FakeBrowserService(navigate_error=PlaywrightError("navigation timeout")),
    )
    result = browse_tool.run({"url": "http://example.com"}, ctx)
    assert result["ok"] is False
    assert "navigation timeout" in result["output"]


def test_download_file_tool_catches_playwright_error(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(
        task_id=1,
        session=None,
        sandbox_manager=manager,
        browser_service=_FakeBrowserService(download_error=PlaywrightError("net::ERR_CONNECTION_REFUSED")),
    )
    result = download_file_tool.run({"url": "http://example.com/file.bin", "save_as": "out.bin"}, ctx)
    assert result["ok"] is False
    assert "ERR_CONNECTION_REFUSED" in result["output"]
