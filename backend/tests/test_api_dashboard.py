from app.agent.model_adapter import FakeModelAdapter
from app.api import tasks as tasks_module
from app.db import get_session_factory
from app.models import Task
from tests.test_api_tasks import wait_for_status


def _client_session(client):
    """Returns a new Session bound to the same in-memory DB the TestClient's
    app is actually using (the client fixture overrides get_session_factory
    with its own engine, separate from the session_factory fixture)."""
    factory = client.app.dependency_overrides[get_session_factory]()
    return factory()


def test_dashboard_groups_tasks_by_status(client):
    # queued: block the queue with a running task first, then enqueue a second
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    running_resp = client.post("/tasks", json={"description": "running task", "full_auto": False})
    running_id = running_resp.json()["id"]
    assert wait_for_status(client, running_id, "awaiting_answer") == "awaiting_answer"

    # insert a plain queued task directly (not via adapter, so it never runs)
    session = _client_session(client)
    try:
        queued = Task(description="queued task", status="queued", full_auto=False)
        session.add(queued)
        session.commit()
        queued_id = queued.id

        running = Task(description="genuinely running", status="running", full_auto=False)
        session.add(running)
        session.commit()
        running_only_id = running.id

        done = Task(description="done task", status="completed", full_auto=False)
        session.add(done)
        session.commit()
        done_id = done.id
    finally:
        session.close()

    body = client.get("/dashboard").json()
    assert "kill_switch" in body
    assert len(body["queued"]) == 1
    assert body["queued"][0]["id"] == queued_id
    assert len(body["running"]) == 1
    assert body["running"][0]["id"] == running_only_id
    assert len(body["needs_you"]) == 1
    assert body["needs_you"][0]["id"] == running_id
    assert len(body["done"]) == 1
    assert body["done"][0]["id"] == done_id


def test_dashboard_nests_children_under_parent(client):
    session = _client_session(client)
    try:
        parent = Task(description="parent", status="running", full_auto=False)
        session.add(parent)
        session.commit()
        child = Task(
            description="child",
            status="queued",
            full_auto=False,
            parent_task_id=parent.id,
            agent_type="researcher",
        )
        session.add(child)
        session.commit()
        parent_id, child_id = parent.id, child.id
    finally:
        session.close()

    body = client.get("/dashboard").json()
    running_entry = next(t for t in body["running"] if t["id"] == parent_id)
    assert len(running_entry["children"]) == 1
    assert running_entry["children"][0]["id"] == child_id
    assert running_entry["children"][0]["agent_type"] == "researcher"


def test_dashboard_done_list_is_paginated(client):
    session = _client_session(client)
    try:
        for i in range(55):
            session.add(Task(description=f"done {i}", status="completed", full_auto=False))
        session.commit()
    finally:
        session.close()

    body = client.get("/dashboard").json()
    assert len(body["done"]) == 50
