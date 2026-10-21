def test_stream_sends_existing_events_on_connect(client):
    create_response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]

    expected_events = client.get(f"/tasks/{task_id}").json()["events"]
    assert len(expected_events) >= 2  # model_step + reflection, at minimum

    with client.websocket_connect(f"/tasks/{task_id}/stream") as websocket:
        received = [websocket.receive_json() for _ in expected_events]

    assert len(received) == len(expected_events)
    assert [e["type"] for e in received] == [e["type"] for e in expected_events]
