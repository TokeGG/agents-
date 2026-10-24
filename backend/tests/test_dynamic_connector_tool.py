from app.agent.tools import ToolContext
from app.connectors.dynamic_tool import DynamicConnectorTool
from app.connectors.registry import Connector
from app.models import Task
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime
from app.vault import Vault


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
