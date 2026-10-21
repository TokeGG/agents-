import time

from app.agent.model_adapter import FakeModelAdapter
from app.api import tasks as tasks_module
from tests.test_api_tasks import wait_for_status


class _SlowTool:
    """A tool whose run() blocks for a bit in the worker thread, so the task
    stays genuinely "running" (not parked in an awaiting_* status) long
    enough for a concurrent request to observe and kill it mid-flight."""

    name = "slow_tool"
    description = "a slow no-op tool, for tests"
    parameters_schema = {"type": "object", "properties": {}}

    def run(self, arguments, ctx):
        time.sleep(0.3)
        return {"ok": True, "output": "done", "data": None}


def _registry_with_slow_tool():
    from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool

    registry = ToolRegistry()
    registry.register(http_get_tool)
    registry.register(ask_user_tool)
    registry.register(report_gap_tool)
    registry.register(_SlowTool())
    return registry


def test_kill_switch_endpoint_returns_status(client):
    response = client.post("/kill-switch", json={"mode": "pause"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "pause"
    assert "running" in body
    assert "cap" in body

    # restore normal mode so other tests in this session aren't affected
    client.post("/kill-switch", json={"mode": "normal"})


def test_kill_task_endpoint_interrupts_waiting_task(client):
    """Killing a task parked in awaiting_answer (not a worker in flight —
    nothing in Scheduler._interrupt_events for it): covers finding #1's
    direct-interrupt path, not the running-task drain path (see
    test_kill_task_endpoint_interrupts_genuinely_running_task below for
    that)."""
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    create_response = client.post("/tasks", json={"description": "pick one", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "awaiting_answer") == "awaiting_answer"

    kill_response = client.post(f"/tasks/{task_id}/kill")
    assert kill_response.status_code == 200
    body = kill_response.json()
    assert body["id"] == task_id
    assert body["status"] == "interrupted"

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["task"]["status"] == "interrupted"
    gap_events = [e for e in detail["events"] if e["type"] == "report_gap"]
    assert gap_events, "expected a report_gap event recording the kill"


def test_kill_task_endpoint_interrupts_genuinely_running_task(client):
    """A task actually executing mid-loop in a worker thread (status
    "running", registered in Scheduler._interrupt_events) killed via the real
    HTTP route: covers finding #2's drain path, not just the waiting-task
    direct-interrupt path above."""
    client.app.dependency_overrides[tasks_module.get_registry] = _registry_with_slow_tool
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "slow_tool", "arguments": {}}]
    )
    create_response = client.post("/tasks", json={"description": "run slowly", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "running") == "running"

    kill_response = client.post(f"/tasks/{task_id}/kill")
    assert kill_response.status_code == 200
    body = kill_response.json()
    assert body["id"] == task_id
    assert body["status"] == "interrupted"

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["task"]["status"] == "interrupted"
    gap_events = [e for e in detail["events"] if e["type"] == "report_gap"]
    assert gap_events, "expected a report_gap event recording the kill"


def test_kill_switch_rejects_invalid_mode(client):
    response = client.post("/kill-switch", json={"mode": "not-a-real-mode"})
    assert response.status_code == 422
