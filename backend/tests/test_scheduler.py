import asyncio
import threading

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


@pytest.mark.asyncio
async def test_stop_sets_interrupt_and_waits_for_worker_without_closing_its_session(
    db_session, session_factory
):
    """stop() must not cancel an in-flight run (that would only cancel the
    coroutine awaiting asyncio.to_thread, not the real OS thread running
    run_fn, and would close/pop the session out from under that still-running
    thread). Instead it should signal interrupt_event and wait for the worker
    to actually finish using its session before returning."""
    observed_interrupt = threading.Event()
    session_was_still_open = threading.Event()

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None):
        # Block (in a real OS thread, like the real run_fn does) until stop()
        # signals us, then keep using the session for a bit afterward.
        interrupt_event.wait(timeout=5)
        if interrupt_event.is_set():
            observed_interrupt.set()
        # If stop() had already closed this session, this would raise.
        task = session.get(Task, task_id)
        task.status = "interrupted"
        session.commit()
        session_was_still_open.set()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    await scheduler.start()
    task_id = make_queued_task(db_session)
    scheduler.enqueue(db_session, task_id)
    await asyncio.sleep(0.05)  # let the Scheduler pick it up and start the worker thread

    await scheduler.stop()

    assert observed_interrupt.is_set()
    assert session_was_still_open.is_set()
    assert scheduler._running == {}
    assert scheduler._interrupt_events == {}

    check_session = session_factory()
    try:
        assert check_session.get(Task, task_id).status == "interrupted"
    finally:
        check_session.close()


def test_urgent_task_is_popped_before_older_non_urgent_tasks(db_session, session_factory):
    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=1)
    old1 = make_queued_task(db_session)
    old2 = make_queued_task(db_session)
    urgent = make_queued_task(db_session, urgent=True)
    for tid in (old1, old2, urgent):
        scheduler.enqueue(db_session, tid)
    popped = scheduler._pop_next(db_session)
    assert popped.id == urgent
