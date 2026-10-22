import threading

import httpx

from app.agent.loop import run_task_loop
from app.agent.model_adapter import FakeModelAdapter
from app.agent.tools import ToolRegistry, ask_user_tool, delegate_task_tool, http_get_tool, report_gap_tool
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime, SandboxSlotUnavailable
from tests.test_tools import FakeSchedulerSpy
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


def test_loop_pauses_on_sandbox_slot_unavailable(db_session, task):
    class _AlwaysUnavailableTool:
        name = "run_shell"
        description = "x"
        parameters_schema = {"type": "object", "properties": {}}

        def run(self, arguments, ctx):
            raise SandboxSlotUnavailable(ctx.task_id)

    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "run_shell", "arguments": {}},
    ])
    registry = ToolRegistry()
    registry.register(_AlwaysUnavailableTool())
    run_task_loop(task.id, db_session, adapter, registry)
    assert db_session.get(Task, task.id).status == "awaiting_sandbox_slot"


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


def test_loop_pauses_on_blocking_delegate_task(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "delegate_task", "arguments": {"description": "x", "mode": "blocking"}},
    ])
    registry = ToolRegistry()
    registry.register(delegate_task_tool)
    scheduler = FakeSchedulerSpy()
    run_task_loop(task.id, db_session, adapter, registry, scheduler=scheduler)
    assert db_session.get(Task, task.id).status == "awaiting_subtask"


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


class _AskUserThatGetsKilledMidCall:
    """ask_user whose run() lands a kill (interrupt_event.set()) while the
    tool call is in flight -- after the top-of-loop interrupt check already
    passed for this step, but before the loop sets awaiting_answer."""

    name = "ask_user"
    description = "ask"
    parameters_schema = {"type": "object", "properties": {"question": {"type": "string"}}}

    def __init__(self, interrupt_event):
        self.interrupt_event = interrupt_event

    def run(self, arguments, ctx):
        result = ask_user_tool.run(arguments, ctx)
        self.interrupt_event.set()
        return result


def test_kill_landing_during_ask_user_step_marks_interrupted_not_awaiting_answer(db_session, task):
    event = threading.Event()
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "q"}},
    ])
    registry = ToolRegistry()
    registry.register(_AskUserThatGetsKilledMidCall(event))
    run_task_loop(task.id, db_session, adapter, registry, interrupt_event=event)
    assert db_session.get(Task, task.id).status == "interrupted"
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "stopped by kill switch" in gap.payload["reason"]


class _DelegateThatGetsKilledMidCall:
    name = "delegate_task"
    description = "delegate"
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self, interrupt_event):
        self.interrupt_event = interrupt_event

    def run(self, arguments, ctx):
        result = delegate_task_tool.run(arguments, ctx)
        self.interrupt_event.set()
        return result


def test_kill_landing_during_blocking_delegate_marks_interrupted_not_awaiting_subtask(db_session, task):
    event = threading.Event()
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "delegate_task", "arguments": {"description": "x", "mode": "blocking"}},
    ])
    registry = ToolRegistry()
    registry.register(_DelegateThatGetsKilledMidCall(event))
    run_task_loop(task.id, db_session, adapter, registry, scheduler=FakeSchedulerSpy(), interrupt_event=event)
    assert db_session.get(Task, task.id).status == "interrupted"


class _KillDuringStepAdapter:
    """Lands the kill during the model step itself (after the top-of-loop
    check passed), and that step's tool needs approval."""

    def __init__(self, interrupt_event):
        self.interrupt_event = interrupt_event

    def complete(self, messages, specs):
        self.interrupt_event.set()
        return {"type": "tool_call", "tool_name": "send_email", "arguments": {}}


def test_kill_landing_during_step_needing_approval_marks_interrupted(db_session, task):
    event = threading.Event()
    registry = ToolRegistry()
    registry.register(_RaisingSendEmailTool())
    run_task_loop(task.id, db_session, _KillDuringStepAdapter(event), registry, interrupt_event=event)
    assert db_session.get(Task, task.id).status == "interrupted"


class _AlwaysFailsTool:
    name = "run_shell"
    description = "x"
    parameters_schema = {"type": "object", "properties": {}}

    def run(self, arguments, ctx):
        raise ValueError("same error every time")


def test_loop_escalates_to_reviewer_after_repeated_failures(db_session, task):
    worker = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "run_shell", "arguments": {}},
    ] * 3)
    reviewer = FakeModelAdapter(script=[{"type": "final_answer", "content": "giving up"}])
    registry = ToolRegistry()
    registry.register(_AlwaysFailsTool())
    run_task_loop(task.id, db_session, worker, registry, reviewer_adapter=reviewer)
    assert db_session.get(Task, task.id).status == "completed"  # reviewer's final_answer was used


def test_post_task_review_runs_once_then_completes_regardless(tmp_path, db_session, task):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)  # task has a sandbox
    worker = FakeModelAdapter(script=[{"type": "final_answer", "content": "done"}])
    reviewer_calls = []

    class _RecordingReviewer:
        def complete(self, messages, tools):
            reviewer_calls.append(messages)
            return {"type": "final_answer", "content": "looks fine"}

    run_task_loop(
        task.id, db_session, worker, ToolRegistry(),
        reviewer_adapter=_RecordingReviewer(), sandbox_manager=manager,
    )
    assert len(reviewer_calls) == 1
    assert db_session.get(Task, task.id).status == "completed"


def test_post_task_review_skipped_without_sandbox_manager(db_session, task):
    worker = FakeModelAdapter(script=[{"type": "final_answer", "content": "done"}])
    reviewer_calls = []

    class _RecordingReviewer:
        def complete(self, messages, tools):
            reviewer_calls.append(messages)
            return {"type": "final_answer", "content": "looks fine"}

    run_task_loop(
        task.id, db_session, worker, ToolRegistry(),
        reviewer_adapter=_RecordingReviewer(), sandbox_manager=None,
    )
    assert reviewer_calls == []
    assert db_session.get(Task, task.id).status == "completed"
