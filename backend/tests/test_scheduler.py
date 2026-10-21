import asyncio

import pytest

from app.models import Task
from app.scheduler import Scheduler


def make_queued_task(session, urgent=False):
    t = Task(description="x", status="queued", full_auto=False, urgent=urgent)
    session.add(t)
    session.commit()
    return t.id


@pytest.mark.asyncio
async def test_cap_enforced_and_fourth_task_waits(db_session, session_factory):
    started = []

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None):
        started.append(task_id)
        await asyncio.sleep(0.05)
        task = session.get(Task, task_id)
        task.status = "completed"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    await scheduler.start()
    task_ids = [make_queued_task(db_session) for _ in range(4)]
    for tid in task_ids:
        scheduler.enqueue(db_session, tid)
    await asyncio.sleep(0.02)
    assert len(started) == 3  # 4th hasn't started yet, cap holds
    await asyncio.sleep(0.1)
    assert len(started) == 4  # 4th started once a slot freed
    await scheduler.stop()


def test_urgent_task_is_popped_before_older_non_urgent_tasks(db_session, session_factory):
    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=1)
    old1 = make_queued_task(db_session)
    old2 = make_queued_task(db_session)
    urgent = make_queued_task(db_session, urgent=True)
    for tid in (old1, old2, urgent):
        scheduler.enqueue(db_session, tid)
    popped = scheduler._pop_next(db_session)
    assert popped.id == urgent
