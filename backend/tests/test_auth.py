import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_request_without_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    bare_client = TestClient(client.app)  # no Authorization header at all
    response = bare_client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.status_code == 401


def test_request_with_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    wrong_client = TestClient(client.app, headers={"Authorization": "Bearer wrong-token"})
    response = wrong_client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.status_code == 401


def test_request_with_correct_token_is_accepted(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    authed_client = TestClient(client.app, headers={"Authorization": "Bearer secret-token"})
    response = authed_client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.status_code == 201


def test_websocket_without_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    authed_client = TestClient(client.app, headers={"Authorization": "Bearer secret-token"})
    create_response = authed_client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]

    bare_client = TestClient(client.app)
    with pytest.raises(WebSocketDisconnect):
        with bare_client.websocket_connect(f"/tasks/{task_id}/stream"):
            pass


def test_websocket_with_correct_token_query_param_is_accepted(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    authed_client = TestClient(client.app, headers={"Authorization": "Bearer secret-token"})
    create_response = authed_client.post("/tasks", json={"description": "say hello", "full_auto": False})
    task_id = create_response.json()["id"]

    bare_client = TestClient(client.app)
    with bare_client.websocket_connect(f"/tasks/{task_id}/stream?token=secret-token") as ws:
        ws.close()
