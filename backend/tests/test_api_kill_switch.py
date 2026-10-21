from app.agent.model_adapter import FakeModelAdapter
from app.api import tasks as tasks_module
from tests.test_api_tasks import wait_for_status


def test_kill_switch_endpoint_returns_status(client):
    response = client.post("/kill-switch", json={"mode": "pause"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "pause"
    assert "running" in body
    assert "cap" in body

    # restore normal mode so other tests in this session aren't affected
    client.post("/kill-switch", json={"mode": "normal"})


def test_kill_task_endpoint_interrupts_running_task(client):
    client.app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which?"}}]
    )
    create_response = client.post("/tasks", json={"description": "pick one", "full_auto": False})
    task_id = create_response.json()["id"]
    assert wait_for_status(client, task_id, "awaiting_answer") == "awaiting_answer"

    kill_response = client.post(f"/tasks/{task_id}/kill")
    assert kill_response.status_code == 200
    assert kill_response.json()["id"] == task_id
