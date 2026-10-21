import threading

import httpx

from app.agent.loop import run_task_loop
from app.agent.model_adapter import FakeModelAdapter
from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool
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


def test_loop_records_malformed_json_tool_call_and_continues(db_session, task):
    """Review Focus: a model_step whose arguments failed JSON parsing (the
    adapter's malformed_tool_call step) must not silently complete the task —
    it is recorded as a failed observation and the loop continues."""
    adapter = FakeModelAdapter(script=[
        {"type": "malformed_tool_call", "raw": "{not valid json"},
        {"type": "final_answer", "content": "done"},
    ])
    registry = ToolRegistry()
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "completed"
    failed_result = db_session.query(Event).filter_by(task_id=task.id, type="tool_result").first()
    assert failed_result.payload["ok"] is False
    assert "not valid json" in failed_result.payload["output"]


def test_loop_fails_with_report_gap_when_step_limit_exceeded(db_session, task):
    """Review Focus: a model that keeps calling tools/report_gap forever must
    not run unbounded — the loop enforces a max-step cap."""
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "report_gap", "arguments": {"reason": "stuck", "options": ["x"]}}
    ] * 100)
    registry = ToolRegistry()
    registry.register(report_gap_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "failed"
    gaps = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").all()
    assert len(gaps) < 100
    assert any("maximum step limit" in g.payload["reason"] for g in gaps)


def test_loop_stops_immediately_when_interrupt_event_already_set(db_session, task):
    adapter = FakeModelAdapter(script=[{"type": "final_answer", "content": "should not run"}])
    registry = ToolRegistry()
    event = threading.Event()
    event.set()
    run_task_loop(task.id, db_session, adapter, registry, interrupt_event=event)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "interrupted"
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "stopped by kill switch" in gap.payload["reason"]


def test_steps_taken_persists_across_resume(db_session, task):
    adapter1 = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "q"}},
    ])
    registry = ToolRegistry()
    registry.register(ask_user_tool)
    run_task_loop(task.id, db_session, adapter1, registry)
    after_first = db_session.get(Task, task.id).steps_taken
    assert after_first == 1

    adapter2 = FakeModelAdapter(script=[{"type": "final_answer", "content": "done"}])
    run_task_loop(task.id, db_session, adapter2, registry)
    assert db_session.get(Task, task.id).steps_taken == 2
