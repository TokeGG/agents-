import asyncio

import pytest

from app.agent.loop import run_task_loop
from app.agent.model_adapter import FakeModelAdapter
from app.agent.tools import ToolRegistry, delegate_task_tool, report_gap_tool
from app.models import Event, Task
from app.scheduler import Scheduler


def make_queued_task(session, description="parent task"):
    t = Task(description=description, status="queued", full_auto=False)
    session.add(t)
    session.commit()
    return t.id


@pytest.mark.asyncio
async def test_blocking_parent_resumes_with_child_result(db_session, session_factory):
    """Drives a parent that delegates (mode=blocking) and a child that
    immediately finishes through a real Scheduler, and verifies the parent
    is automatically re-enqueued and reaches "completed" once the child is
    done -- proving the _on_task_terminal resume path end-to-end."""

    parent_id = make_queued_task(db_session, "parent task")

    # First adapter call for the parent issues the blocking delegate_task;
    # once resumed (after the child finishes), the second call is a plain
    # final_answer using the subtask's result.
    parent_adapter = FakeModelAdapter(script=[
        {
            "type": "tool_call",
            "tool_name": "delegate_task",
            "arguments": {"description": "child work", "mode": "blocking"},
        },
        {"type": "final_answer", "content": "done, child said done"},
    ])
    child_adapter = FakeModelAdapter(script=[
        {"type": "final_answer", "content": "done"},
    ])

    class _RoutingAdapter:
        """The scheduler calls adapter_factory() once per _run_one invocation,
        before it knows which task_id it's for, so dispatch by inspecting the
        first (user) message's content, which is the task's description --
        the only thing distinguishing "parent task" from a delegated child at
        that point."""

        def __init__(self, parent_adapter, fallback_adapter):
            self._parent_adapter = parent_adapter
            self._fallback = fallback_adapter

        def complete(self, messages, tools):
            description = messages[0]["content"]
            if description == "parent task":
                return self._parent_adapter.complete(messages, tools)
            return self._fallback.complete(messages, tools)

    def adapter_factory():
        return _RoutingAdapter(parent_adapter, child_adapter)

    def registry_factory():
        registry = ToolRegistry()
        registry.register(delegate_task_tool)
        registry.register(report_gap_tool)
        return registry

    scheduler = Scheduler(session_factory, adapter_factory, registry_factory, cap=2)
    await scheduler.start()
    scheduler.enqueue(db_session, parent_id)

    deadline = asyncio.get_event_loop().time() + 5.0
    parent_task = db_session.get(Task, parent_id)
    while asyncio.get_event_loop().time() < deadline:
        db_session.expire_all()
        parent_task = db_session.get(Task, parent_id)
        if parent_task.status == "completed":
            break
        await asyncio.sleep(0.02)

    await scheduler.stop()

    assert parent_task.status == "completed"

    parent_events = db_session.query(Event).filter_by(task_id=parent_id).order_by(Event.id).all()
    subtask_update_events = [e for e in parent_events if e.type == "subtask_update"]
    assert len(subtask_update_events) == 1
    assert subtask_update_events[0].payload["status"] == "completed"
