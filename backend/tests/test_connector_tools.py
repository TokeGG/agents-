from pathlib import Path

from app.agent.tools import (
    ToolContext,
    register_connector_tool,
    request_connector_tool,
    vault_provision_test_credential_tool,
)
from app.connectors.registry import approve, create_pending
from app.models import Connector, Task
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime
from app.vault import Vault


class FakeScheduler:
    """Records enqueue(session, task_id, front) calls."""

    def __init__(self):
        self.enqueued = []

    def enqueue(self, session, task_id, front=False):
        self.enqueued.append(task_id)


def test_request_connector_delegates_coding_subtask_when_none_exists(db_session, task):
    scheduler = FakeScheduler()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert result["ok"] is True
    assert len(scheduler.enqueued) == 1
    child = db_session.get(Task, scheduler.enqueued[0])
    assert child.agent_type == "coding"
    assert "github" in child.description


def test_request_connector_short_circuits_active_and_pending(db_session, task):
    create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="create issues", parameters_schema={}, source_code="",
        required_hosts=[], requested_by_task_id=task.id, test_transcript={},
    )
    scheduler = FakeScheduler()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    pending_result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert pending_result["ok"] is True
    assert scheduler.enqueued == []

    approve(db_session, db_session.query(Connector).one().id, approved_by="user")
    active_result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert active_result["data"]["connector_name"] == "github_create_issue"
    assert scheduler.enqueued == []


def test_register_connector_inserts_pending_row(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": "print('hi')",
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        ctx,
    )
    assert result["ok"] is True
    row = db_session.query(Connector).one()
    assert row.status == "pending_review"
    assert row.requested_by_task_id == task.id


def test_register_connector_accepts_malformed_parameters_schema(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = register_connector_tool.run(
        {
            "name": "weird_tool", "service_name": "weird", "purpose": "x",
            "parameters_schema": {"not_a_real_schema": True}, "source_code": "",
            "required_hosts": [], "test_transcript": {},
        },
        ctx,
    )
    assert result["ok"] is True
    row = db_session.query(Connector).filter_by(name="weird_tool").one()
    assert row.status == "pending_review"
    assert row.parameters_schema == {"not_a_real_schema": True}


def test_vault_provision_test_credential_present_and_absent(tmp_path, db_session, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "ghp_abc123")
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    present = vault_provision_test_credential_tool.run({"service_name": "github"}, ctx)
    assert present["ok"] is True
    assert "ghp_abc123" not in present["output"]
    assert present["data"]["path"].startswith("secrets/")

    absent = vault_provision_test_credential_tool.run({"service_name": "slack"}, ctx)
    assert absent["ok"] is True
    assert absent["data"]["path"] is None
