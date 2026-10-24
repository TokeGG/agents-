import json

import pytest

from app.agent.tools import ToolContext
from app.connectors.dynamic_tool import DynamicConnectorTool
from app.connectors.registry import Connector
from app.models import Task
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime
from app.vault import Vault


class _DelegatingManager:
    """Wraps a real SandboxManager, intercepting only `run_shell` calls for
    the connector invocation (identified by the stdin-feeding `__ARGS_`
    heredoc marker DynamicConnectorTool uses) so tests can simulate a
    timeout or a raised exception from exactly that call, while mkdir/
    write_file/cleanup calls go through untouched."""

    def __init__(self, inner, on_invocation):
        self._inner = inner
        self._on_invocation = on_invocation

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def run_shell(self, task_id, command, timeout=120):
        if "__ARGS_" in command:
            return self._on_invocation(task_id, command, timeout)
        return self._inner.run_shell(task_id, command, timeout=timeout)


def test_dynamic_connector_tool_no_sandbox_manager_fails_gracefully(db_session, task):
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code="print('hi')",
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=None)
    result = tool.run({}, ctx)
    assert result["ok"] is False


def test_dynamic_connector_tool_runs_without_credential(tmp_path, db_session, task):
    source = (
        "import sys, json\n"
        "args = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'output': f\"got {args}\", 'data': None}))\n"
    )
    connector = Connector(name="echo_tool", service_name="echo", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager)
    result = tool.run({"x": 1}, ctx)
    assert result["ok"] is True
    assert "got" in result["output"]


def test_dynamic_connector_tool_provisions_and_shreds_credential(tmp_path, db_session, task):
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': f'used token {secret}', 'data': None}))\n"
    )
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "s3cr3t-value")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result["ok"] is True
    assert "s3cr3t-value" not in result["output"]
    assert "***REDACTED***" in result["output"]
    # the secret file must not survive the call
    volume_dir = tmp_path / manager._volume_name(task.id)
    assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))


def test_dynamic_connector_tool_handles_rotated_vault_key(tmp_path, db_session, session_factory, task):
    """If VAULT_ENCRYPTION_KEY was rotated without re-provisioning existing
    secrets, Vault.get raises InvalidToken on the old ciphertext. The tool
    must turn that into a normal failure result, not propagate the
    exception."""
    source = (
        "import sys, json\n"
        "print(json.dumps({'ok': True, 'output': 'should never run', 'data': None}))\n"
    )
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)

    writer_vault = Vault(Vault.generate_key(), session_factory=session_factory)
    writer_vault.set("github", "s3cr3t-value")

    reader_vault = Vault(Vault.generate_key(), session_factory=session_factory)
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=reader_vault)

    result = tool.run({}, ctx)
    assert result["ok"] is False
    assert result["data"] is None
    assert "github" in result["output"]
    assert "re-provision" in result["output"]


def test_dynamic_connector_tool_isolates_cache_and_secret_per_task(tmp_path, db_session, session_factory):
    task_a = Task(description="a", status="running"); db_session.add(task_a)
    task_b = Task(description="b", status="running"); db_session.add(task_b)
    db_session.commit()
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': f'used {secret}', 'data': None}))\n"
    )
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=3)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", "secret-for-both-tasks")
    tool = DynamicConnectorTool(connector)

    ctx_a = ToolContext(task_id=task_a.id, session=db_session, sandbox_manager=manager, vault=vault)
    ctx_b = ToolContext(task_id=task_b.id, session=db_session, sandbox_manager=manager, vault=vault)
    result_a = tool.run({}, ctx_a)
    result_b = tool.run({}, ctx_b)
    assert result_a["ok"] is True and result_b["ok"] is True

    # each task's own sandbox volume has its own cached source copy
    assert (tmp_path / manager._volume_name(task_a.id) / "connectors" / "github_create_issue.py").exists()
    assert (tmp_path / manager._volume_name(task_b.id) / "connectors" / "github_create_issue.py").exists()
    # and neither task's volume has a leftover secret file after both calls
    for tid in (task_a.id, task_b.id):
        volume_dir = tmp_path / manager._volume_name(tid)
        assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))


def test_dynamic_connector_tool_redacts_secret_from_data_field_too(tmp_path, db_session, task):
    """Redaction must cover the WHOLE parsed result, not just `output`: a
    connector that echoes the secret into `data` must not leak it there."""
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': 'done', 'data': {'token': secret}}))\n"
    )
    connector = Connector(name="leaky_tool", service_name="leaky", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("leaky", "s3cr3t-in-data")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result["ok"] is True
    assert "s3cr3t-in-data" not in json.dumps(result)
    assert result["data"]["token"] == "***REDACTED***"


def test_dynamic_connector_tool_redacts_json_escaped_secret_in_nested_data(tmp_path, db_session, task):
    """A secret containing characters json.dumps escapes (a double-quote,
    here) must still be redacted even though the raw JSON text contains the
    escaped form (`\\"`), not the literal secret -- the raw-text redaction
    pass alone cannot catch this; it requires walking the parsed structure
    and comparing against the literal (decoded) secret value. Also nests
    the secret inside a list inside `data` to prove the recursive walk
    reaches lists, not just dict values."""
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': 'done', 'data': {'items': [secret]}}))\n"
    )
    connector = Connector(name="quote_leaky_tool", service_name="quote_leaky", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    secret_value = 'secret-with-"-quote'
    vault.set("quote_leaky", secret_value)
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result["ok"] is True
    assert secret_value not in json.dumps(result)
    assert result["data"]["items"] == ["***REDACTED***"]


def test_dynamic_connector_tool_shreds_secret_on_timeout(tmp_path, db_session, task):
    """A command timeout kills the whole shell session (including the
    in-shell `rm -f`) before it runs; the finally-block cleanup must still
    remove the secret file."""
    source = "import time\ntime.sleep(5)\n"
    connector = Connector(name="slow_tool", service_name="slow", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    real_manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)

    def on_invocation(task_id, command, timeout):
        return real_manager.run_shell(task_id, command, timeout=1)

    manager = _DelegatingManager(real_manager, on_invocation)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("slow", "s3cr3t-timeout")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result["ok"] is False
    volume_dir = tmp_path / real_manager._volume_name(task.id)
    assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))


def test_dynamic_connector_tool_shreds_secret_when_run_shell_raises(tmp_path, db_session, task):
    """If run_shell itself raises (e.g. a stopped container, a Docker API
    error) after the secret file was already provisioned, the finally-block
    cleanup must still remove it, even though the exception propagates."""
    connector = Connector(name="raise_tool", service_name="raise_svc", purpose="x",
                           parameters_schema={"type": "object"}, source_code="print('hi')",
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    real_manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)

    def on_invocation(task_id, command, timeout):
        raise RuntimeError("container exploded")

    manager = _DelegatingManager(real_manager, on_invocation)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("raise_svc", "s3cr3t-raise")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    with pytest.raises(RuntimeError):
        tool.run({}, ctx)

    volume_dir = tmp_path / real_manager._volume_name(task.id)
    assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))


def test_dynamic_connector_tool_provision_failure_returns_normal_failure_shape(tmp_path, db_session, task):
    """provision_secret_file's RuntimeError must not escape run() uncaught;
    it should become the same {"ok": False, ...} shape every other tool
    failure path uses."""
    connector = Connector(name="broken_cred_tool", service_name="broken", purpose="x",
                           parameters_schema={"type": "object"}, source_code="print('hi')",
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()

    class _FailingWriteManager:
        """A sandbox manager whose write_file always fails, so
        provision_secret_file raises RuntimeError (per its own contract)."""

        def run_shell(self, task_id, command, timeout=120):
            return {"ok": True, "output": "", "data": None}

        def write_file(self, task_id, path, content):
            if path.startswith("secrets/"):
                return {"ok": False, "output": "disk full", "data": None}
            return {"ok": True, "output": "", "data": None}

    manager = _FailingWriteManager()
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("broken", "s3cr3t-broken")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result == {"ok": False, "output": "failed to provision credential: disk full", "data": None}
