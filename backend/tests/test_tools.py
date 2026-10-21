import httpx

from app.agent.tools import (
    MAX_CHILDREN_PER_TASK,
    MAX_DELEGATION_DEPTH,
    ToolContext,
    ToolRegistry,
    check_subtask_tool,
    delegate_task_tool,
    http_get_tool,
    report_gap_tool,
)
from app.models import Event, Task


class FakeSchedulerSpy:
    """Records enqueue(session, task_id, front) calls."""

    def __init__(self):
        self.enqueued = []
        self.calls = []

    def enqueue(self, session, task_id, front=False):
        self.enqueued.append(task_id)
        self.calls.append((task_id, front))


def test_registry_lookup():
    registry = ToolRegistry()
    registry.register(http_get_tool)
    assert registry.get("http_get") is http_get_tool
    assert registry.get("nonexistent") is None
    assert any(s["name"] == "http_get" for s in registry.specs())


def test_report_gap_persists_event(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = report_gap_tool.run(
        {"reason": "no API key for X", "options": ["add key to vault", "use browser instead"]}, ctx
    )
    assert result["ok"] is True
    event = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert event.payload["options"] == ["add key to vault", "use browser instead"]


def test_http_get_tool_returns_status_in_output(db_session, task, monkeypatch):
    ctx = ToolContext(task_id=task.id, session=db_session)

    def fake_get(url):
        return httpx.Response(200, text="hello", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    result = http_get_tool.run({"url": "http://x"}, ctx)

    assert result["ok"] is True
    assert "200" in result["output"]


def test_delegate_task_creates_and_enqueues_child(db_session, task):
    scheduler = FakeSchedulerSpy()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    result = delegate_task_tool.run({"description": "do X", "mode": "background"}, ctx)
    assert result["ok"] is True
    child_id = result["data"]["subtask_id"]
    child = db_session.get(Task, child_id)
    assert child.parent_task_id == task.id
    assert child.delegation_depth == 1
    assert scheduler.enqueued == [child_id]


def test_delegate_task_refuses_past_depth_limit(db_session, task):
    task.delegation_depth = MAX_DELEGATION_DEPTH
    db_session.commit()
    scheduler = FakeSchedulerSpy()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    before_count = db_session.query(Task).count()
    result = delegate_task_tool.run({"description": "too deep", "mode": "background"}, ctx)
    assert result["ok"] is False
    assert db_session.query(Task).count() == before_count
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "too deeply nested" in gap.payload["reason"]


def test_delegate_task_refuses_past_fanout_limit(db_session, task):
    for _ in range(MAX_CHILDREN_PER_TASK):
        db_session.add(Task(description="c", status="queued", parent_task_id=task.id))
    db_session.commit()
    scheduler = FakeSchedulerSpy()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    before_count = db_session.query(Task).count()
    result = delegate_task_tool.run({"description": "one too many", "mode": "background"}, ctx)
    assert result["ok"] is False
    assert db_session.query(Task).count() == before_count
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "too many" in gap.payload["reason"]


def test_check_subtask_returns_status(db_session, task):
    child = Task(description="child", status="running", parent_task_id=task.id)
    db_session.add(child)
    db_session.commit()
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = check_subtask_tool.run({"subtask_id": child.id}, ctx)
    assert result["ok"] is True
    assert result["data"]["status"] == "running"


def test_check_subtask_not_found(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = check_subtask_tool.run({"subtask_id": 999999}, ctx)
    assert result["ok"] is False
    assert result["data"] is None
