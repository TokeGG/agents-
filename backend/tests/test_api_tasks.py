import time

from app.agent.model_adapter import FakeModelAdapter
from app.api import tasks as tasks_module


def wait_for_status(client, task_id, *statuses, timeout=10.0):
    """Polls GET /tasks/{id} until its status is one of `statuses` (the real
    async Scheduler runs tasks on its own poll loop, not synchronously within
    the request), returning the final status seen."""
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/tasks/{task_id}").json()["task"]["status"]
        if status in statuses:
            return status
        time.sleep(0.02)
    return status


def test_create_and_get_task(client):
    response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"

    assert wait_for_status(client, body["id"], "completed") == "completed"
    get_response = client.get(f"/tasks/{body['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["task"]["description"] == "say hello"


def test_rate_fails_when_task_not_completed(client):
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    create_response = client.post("/tasks", json={"description": "pick one", "full_auto": False})
    task_id = create_response.json()["id"]

    rate_response = client.post(f"/tasks/{task_id}/rate", json={"value": 1})
    assert rate_response.status_code == 409


def test_rate_succeeds_when_task_completed(client):
    create_response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "completed") == "completed"

    rate_response = client.post(f"/tasks/{task_id}/rate", json={"value": 1})
    assert rate_response.status_code == 201


def test_answer_resumes_task_to_completion(client):
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    create_response = client.post("/tasks", json={"description": "pick one", "full_auto": False})
    task_id = create_response.json()["id"]
    status = wait_for_status(client, task_id, "awaiting_answer")
    if status != "awaiting_answer":
        print("DEBUG", client.get(f"/tasks/{task_id}").json())
    assert status == "awaiting_answer"

    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "final_answer", "content": "done"}]
    )
    answer_response = client.post(f"/tasks/{task_id}/answer", json={"answer": "option A"})
    assert answer_response.status_code == 201
    assert wait_for_status(client, task_id, "completed") == "completed"


def test_answer_accepts_blank_string(client):
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    create_response = client.post("/tasks", json={"description": "pick one", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "awaiting_answer") == "awaiting_answer"

    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "final_answer", "content": "done"}]
    )
    answer_response = client.post(f"/tasks/{task_id}/answer", json={"answer": ""})
    assert answer_response.status_code == 201


def test_approve_denial_does_not_execute_tool_and_records_denial(client):
    class _RaisingSendEmailTool:
        name = "send_email"
        description = "send an email"
        parameters_schema = {"type": "object", "properties": {}}

        def run(self, arguments, ctx):
            raise AssertionError("send_email should not run when approval is denied")

    def registry_with_send_email():
        from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool

        registry = ToolRegistry()
        registry.register(http_get_tool)
        registry.register(ask_user_tool)
        registry.register(report_gap_tool)
        registry.register(_RaisingSendEmailTool())
        return registry

    client.app.dependency_overrides[tasks_module.get_registry] = registry_with_send_email
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "send_email", "arguments": {}}]
    )
    create_response = client.post("/tasks", json={"description": "email someone", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "awaiting_approval") == "awaiting_approval"

    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "final_answer", "content": "done"}]
    )
    approve_response = client.post(f"/tasks/{task_id}/approve", json={"approved": False})
    assert approve_response.status_code == 201

    assert wait_for_status(client, task_id, "completed") == "completed"
    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["task"]["status"] == "completed"
    denial_events = [e for e in detail["events"] if e["type"] == "approval_response"]
    assert denial_events[0]["payload"]["approved"] is False


def test_create_task_marks_failed_when_adapter_raises(client):
    def raising_adapter():
        class _RaisingAdapter:
            def complete(self, messages, tools):
                raise RuntimeError("model API unreachable")

        return _RaisingAdapter()

    client.app.dependency_overrides[tasks_module.get_adapter] = raising_adapter
    create_response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "failed") == "failed"

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["task"]["status"] == "failed"
    gap_events = [e for e in detail["events"] if e["type"] == "report_gap"]
    assert gap_events, "expected a report_gap event recording the internal error"
    assert "model API unreachable" in gap_events[0]["payload"]["reason"]


def test_approve_returns_error_when_no_pending_approval_request(client):
    create_response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]  # this task completes immediately, never awaiting_approval

    # Force the task into awaiting_approval without a approval_request event,
    # to exercise the defensive guard directly.
    from app.db import get_session as real_get_session

    session_gen = client.app.dependency_overrides[real_get_session]()
    session = next(session_gen)
    from app.models import Task

    task = session.get(Task, task_id)
    task.status = "awaiting_approval"
    session.commit()

    approve_response = client.post(f"/tasks/{task_id}/approve", json={"approved": True})
    assert approve_response.status_code == 409


def test_create_task_runs_through_scheduler(client):
    response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.json()["status"] == "queued"
    task_id = response.json()["id"]
    assert wait_for_status(client, task_id, "completed") == "completed"
