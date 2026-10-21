import time

from tests.conftest import TEST_AUTH_TOKEN


def test_stream_sends_existing_events_on_connect(client):
    create_response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]

    # The real async Scheduler runs tasks on its own poll loop, not
    # synchronously within the request, so wait for it to finish. Status and
    # its final events are committed together, but poll a couple more times
    # after first seeing "completed" to be robust to the read landing in the
    # narrow window between the writer's statements within that commit.
    expected_events = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        detail = client.get(f"/tasks/{task_id}").json()
        if detail["task"]["status"] == "completed" and len(detail["events"]) >= 2:
            expected_events = detail["events"]
            break
        time.sleep(0.02)
    assert len(expected_events) >= 2  # model_step + reflection, at minimum

    with client.websocket_connect(f"/tasks/{task_id}/stream?token={TEST_AUTH_TOKEN}") as websocket:
        received = [websocket.receive_json() for _ in expected_events]

    assert len(received) == len(expected_events)
    assert [e["type"] for e in received] == [e["type"] for e in expected_events]
