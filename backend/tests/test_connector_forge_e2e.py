"""End-to-end connector-forge integration test + standing credential-leak
regression test.

This is intentionally a regression test, not a driver for new
implementation: if it fails, the failure identifies a real gap in an
earlier task's (1-7) code, which should be fixed there rather than
weakening this test.
"""

from app.agent.tools import ToolContext, ToolRegistry, register_connector_tool
from app.api.tasks import get_registry
from app.connectors.dynamic_tool import DynamicConnectorTool
from app.connectors.registry import Connector, approve
from app.models import Event
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime
from app.vault import Vault


def test_full_forge_cycle_never_leaks_credential_into_events(tmp_path, db_session, session_factory, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", "s3cr3t-value")

    source = (
        "import sys, json\n"
        "path = sys.argv[1] if len(sys.argv) > 1 else None\n"
        "secret = open(path).read().strip() if path else None\n"
        "print(json.dumps({'ok': True, 'output': f'called github with {secret}', 'data': None}))\n"
    )
    register_ctx = ToolContext(task_id=task.id, session=db_session)
    register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": source,
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        register_ctx,
    )
    connector_row = db_session.query(Connector).one()
    approve(db_session, connector_row.id, approved_by="user")

    registry = ToolRegistry()
    registry.register(DynamicConnectorTool(connector_row))
    tool = registry.get("github_create_issue")
    call_ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)
    result = tool.run({}, call_ctx)
    assert result["ok"] is True
    assert "s3cr3t-value" not in result["output"]

    # Simulate the agent loop persisting this result, same as run_task_loop does
    db_session.add(Event(task_id=task.id, type="tool_result", payload=result))
    db_session.commit()

    all_event_text = " ".join(str(e.payload) for e in db_session.query(Event).filter_by(task_id=task.id).all())
    assert "s3cr3t-value" not in all_event_text


def test_full_forge_cycle_through_get_registry_wiring(tmp_path, db_session, session_factory, task):
    """Same shape as above, but goes through the real get_registry() wiring
    (Task 6) instead of a hand-assembled ToolRegistry/DynamicConnectorTool,
    proving a newly approved connector is actually exposed through the real
    FastAPI dependency wiring."""
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", "s3cr3t-value")

    source = (
        "import sys, json\n"
        "path = sys.argv[1] if len(sys.argv) > 1 else None\n"
        "secret = open(path).read().strip() if path else None\n"
        "print(json.dumps({'ok': True, 'output': f'called github with {secret}', 'data': None}))\n"
    )
    register_ctx = ToolContext(task_id=task.id, session=db_session)
    register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": source,
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        register_ctx,
    )
    connector_row = db_session.query(Connector).one()
    approve(db_session, connector_row.id, approved_by="user")

    # Real wiring: get_registry() builds the ToolRegistry from scratch and
    # registers every active connector, exactly as the FastAPI dependency
    # does per-request.
    registry = get_registry(session=db_session)
    tool = registry.get("github_create_issue")
    assert tool is not None

    call_ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)
    result = tool.run({}, call_ctx)
    assert result["ok"] is True
    assert "s3cr3t-value" not in result["output"]

    db_session.add(Event(task_id=task.id, type="tool_result", payload=result))
    db_session.commit()

    all_event_text = " ".join(str(e.payload) for e in db_session.query(Event).filter_by(task_id=task.id).all())
    assert "s3cr3t-value" not in all_event_text
