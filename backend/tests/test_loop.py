import httpx

from app.agent.loop import run_task_loop
from app.agent.model_adapter import FakeModelAdapter
from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool
from app.models import Event, Task


def test_loop_runs_tool_then_finishes(db_session, task, monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url: httpx.Response(200, text="ok", request=httpx.Request("GET", url))
    )
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "http_get", "arguments": {"url": "http://x"}},
        {"type": "final_answer", "content": "done"},
    ])
    registry = ToolRegistry()
    registry.register(http_get_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "completed"
    assert db_session.query(Event).filter_by(task_id=task.id, type="reflection").count() == 1


def test_loop_pauses_on_ask_user(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which email?"}},
    ])
    registry = ToolRegistry()
    registry.register(ask_user_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    assert db_session.get(Task, task.id).status == "awaiting_answer"


class _RaisingSendEmailTool:
    name = "send_email"
    description = "send an email"
    parameters_schema = {"type": "object", "properties": {}}

    def run(self, arguments, ctx):
        raise AssertionError("send_email should not have been executed without approval")


def test_loop_pauses_on_approval_for_irreversible_tool(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "send_email", "arguments": {}},
    ])
    registry = ToolRegistry()
    registry.register(_RaisingSendEmailTool())
    run_task_loop(task.id, db_session, adapter, registry)
    assert db_session.get(Task, task.id).status == "awaiting_approval"


def test_loop_fails_on_unknown_tool(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "nonexistent_tool", "arguments": {}},
    ])
    registry = ToolRegistry()
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "failed"
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "register this tool via the connector forge (not yet built)" in gap.payload["options"]


def test_loop_records_malformed_tool_call_and_continues(db_session, task, monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url: httpx.Response(200, text="ok", request=httpx.Request("GET", url))
    )
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "http_get", "arguments": {}},  # missing required "url"
        {"type": "final_answer", "content": "done"},
    ])
    registry = ToolRegistry()
    registry.register(http_get_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "completed"
    failed_result = db_session.query(Event).filter_by(task_id=task.id, type="tool_result").first()
    assert failed_result.payload["ok"] is False
