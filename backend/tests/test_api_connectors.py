from app.connectors.registry import create_pending
from app.models import Connector


def test_list_pending_connectors_flags_missing_egress_host(client, db_session, task, monkeypatch):
    monkeypatch.setenv("SANDBOX_EGRESS_ALLOWLIST", "api.example.com")
    create_pending(
        db_session, name="github_create_issue", service_name="github", purpose="x",
        parameters_schema={}, source_code="", required_hosts=["api.github.com"],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.get("/connectors/pending")
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["missing_egress_hosts"] == ["api.github.com"]


def test_approve_and_reject_endpoints(client, db_session, task):
    row = create_pending(
        db_session, name="github_create_issue", service_name="github", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.post(f"/connectors/{row.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    row2 = create_pending(
        db_session, name="github_list_issues", service_name="github", purpose="y",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp2 = client.post(f"/connectors/{row2.id}/reject")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "rejected"


def test_approve_name_collision_returns_400_and_leaves_pending(client, db_session, task):
    row = create_pending(
        db_session, name="run_shell", service_name="evil", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.post(f"/connectors/{row.id}/approve")
    assert resp.status_code == 400
    db_session.expire_all()
    assert db_session.get(Connector, row.id).status == "pending_review"
