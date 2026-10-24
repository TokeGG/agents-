import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.models import Event, Task
from app.scheduler import Scheduler


def make_queued_task(session, urgent=False):
    t = Task(description="x", status="queued", full_auto=False, urgent=urgent)
    session.add(t)
    session.commit()
    return t.id


@pytest.mark.asyncio
async def test_cap_enforced_and_fourth_task_waits(db_session, session_factory):
    started = []

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
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

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
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


@pytest.mark.asyncio
async def test_pause_stops_new_starts_but_lets_running_finish(db_session, session_factory):
    release = threading.Event()
    started = []

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        started.append(task_id)
        release.wait(timeout=5)
        task = session.get(Task, task_id)
        task.status = "completed"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    await scheduler.start()
    running_tid = make_queued_task(db_session)
    scheduler.enqueue(db_session, running_tid)
    await asyncio.sleep(0.05)
    assert started == [running_tid]

    await scheduler.kill_switch("pause")
    tid = make_queued_task(db_session)
    scheduler.enqueue(db_session, tid)
    await asyncio.sleep(0.1)
    assert db_session.get(Task, tid).status == "queued"  # never started
    assert tid not in started

    release.set()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_kill_task_cascades_to_running_child(db_session, session_factory):
    parent_id = make_queued_task(db_session)
    child_id = make_queued_task(db_session)
    parent_task = db_session.get(Task, parent_id)
    parent_task.status = "running"
    child_task = db_session.get(Task, child_id)
    child_task.status = "running"
    child_task.parent_task_id = parent_id
    db_session.commit()

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        interrupt_event.wait(timeout=5)
        task = session.get(Task, task_id)
        task.status = "interrupted"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    scheduler._interrupt_events[parent_id] = threading.Event()
    scheduler._interrupt_events[child_id] = threading.Event()
    scheduler._running[parent_id] = asyncio.create_task(
        asyncio.to_thread(
            fake_run, parent_id, session_factory(), None, None, scheduler,
            scheduler._interrupt_events[parent_id],
        )
    )
    scheduler._running[child_id] = asyncio.create_task(
        asyncio.to_thread(
            fake_run, child_id, session_factory(), None, None, scheduler,
            scheduler._interrupt_events[child_id],
        )
    )

    await scheduler.kill_task(parent_id)
    await asyncio.sleep(0.2)

    db_session.expire_all()
    assert db_session.get(Task, parent_id).status == "interrupted"
    assert db_session.get(Task, child_id).status == "interrupted"


@pytest.mark.asyncio
async def test_kill_task_cascades_to_running_grandchild(db_session, session_factory):
    parent_id = make_queued_task(db_session)
    child_id = make_queued_task(db_session)
    grandchild_id = make_queued_task(db_session)
    db_session.get(Task, parent_id).status = "running"
    child_task = db_session.get(Task, child_id)
    child_task.status = "running"
    child_task.parent_task_id = parent_id
    grandchild_task = db_session.get(Task, grandchild_id)
    grandchild_task.status = "running"
    grandchild_task.parent_task_id = child_id
    db_session.commit()

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        interrupt_event.wait(timeout=5)
        task = session.get(Task, task_id)
        task.status = "interrupted"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    for tid in (parent_id, child_id, grandchild_id):
        scheduler._interrupt_events[tid] = threading.Event()
        scheduler._running[tid] = asyncio.create_task(
            asyncio.to_thread(
                fake_run, tid, session_factory(), None, None, scheduler,
                scheduler._interrupt_events[tid],
            )
        )

    await scheduler.kill_task(parent_id)
    await asyncio.sleep(0.2)

    db_session.expire_all()
    assert db_session.get(Task, parent_id).status == "interrupted"
    assert db_session.get(Task, child_id).status == "interrupted"
    assert db_session.get(Task, grandchild_id).status == "interrupted"


@pytest.mark.asyncio
async def test_kill_task_interrupts_waiting_task_not_currently_running(db_session, session_factory):
    """A task parked in a non-running "waiting" status (queued,
    awaiting_answer, awaiting_approval, awaiting_subtask) has no entry in
    _interrupt_events (nothing is running it right now), so kill_task must
    mark it interrupted directly rather than silently doing nothing."""
    tid = make_queued_task(db_session)
    task = db_session.get(Task, tid)
    task.status = "awaiting_answer"
    db_session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3)
    await scheduler.kill_task(tid)

    db_session.expire_all()
    assert db_session.get(Task, tid).status == "interrupted"


def test_on_task_terminal_does_nothing_when_parent_still_running(db_session, session_factory):
    parent = Task(description="parent", status="running", full_auto=False)
    db_session.add(parent)
    db_session.commit()
    child = Task(description="child", status="completed", parent_task_id=parent.id)
    db_session.add(child)
    db_session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3)
    scheduler._on_task_terminal(child.id)

    db_session.expire_all()
    refreshed_parent = db_session.get(Task, parent.id)
    assert refreshed_parent.status == "running"
    # still queue_position None -- not re-enqueued
    assert refreshed_parent.queue_position is None
    events = db_session.query(Event).filter_by(task_id=parent.id, type="subtask_update").all()
    assert len(events) == 1
    assert events[0].payload["status"] == "completed"


def test_on_task_terminal_reenqueues_parent_awaiting_answer(db_session, session_factory):
    parent = Task(description="parent", status="awaiting_answer", full_auto=False)
    db_session.add(parent)
    db_session.commit()
    child = Task(description="child", status="completed", parent_task_id=parent.id)
    db_session.add(child)
    db_session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3)
    scheduler._on_task_terminal(child.id)

    db_session.expire_all()
    refreshed_parent = db_session.get(Task, parent.id)
    assert refreshed_parent.status == "queued"
    assert refreshed_parent.queue_position is not None


def test_pop_next_skips_task_id_already_running(db_session, session_factory):
    """A task id present in self._running has a run in flight right now and
    must never be popped again, even if its status reads "queued" (e.g. a
    delegation re-enqueue landing while its own first run hasn't finished
    yet) -- otherwise it could be started a second time concurrently."""
    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3)
    tid = make_queued_task(db_session)
    scheduler._running[tid] = object()  # fake in-flight asyncio.Task entry

    assert scheduler._pop_next(db_session) is None

    del scheduler._running[tid]
    assert scheduler._pop_next(db_session).id == tid


def test_urgent_task_is_popped_before_older_non_urgent_tasks(db_session, session_factory):
    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=1)
    old1 = make_queued_task(db_session)
    old2 = make_queued_task(db_session)
    urgent = make_queued_task(db_session, urgent=True)
    for tid in (old1, old2, urgent):
        scheduler.enqueue(db_session, tid)
    popped = scheduler._pop_next(db_session)
    assert popped.id == urgent


@pytest.mark.asyncio
async def test_kill_switch_stop_cascades_to_queued_child_of_running_task(db_session, session_factory):
    parent_id = make_queued_task(db_session)
    child_id = make_queued_task(db_session)
    db_session.get(Task, parent_id).status = "running"
    db_session.get(Task, child_id).parent_task_id = parent_id
    db_session.commit()

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        interrupt_event.wait(timeout=5)
        task = session.get(Task, task_id)
        task.status = "interrupted"
        session.commit()
        session.close()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    scheduler._interrupt_events[parent_id] = threading.Event()
    scheduler._running[parent_id] = asyncio.create_task(
        asyncio.to_thread(
            fake_run, parent_id, session_factory(), None, None, scheduler,
            scheduler._interrupt_events[parent_id],
        )
    )

    await scheduler.kill_switch("stop")

    db_session.expire_all()
    assert db_session.get(Task, parent_id).status == "interrupted"
    assert db_session.get(Task, child_id).status == "interrupted"


@pytest.mark.asyncio
async def test_kill_task_catches_child_created_after_descendant_snapshot(db_session, session_factory):
    """The task being killed creates a new queued child *after* kill_task
    already collected the descendant set (simulating a delegate_task call
    racing the kill). That child must not escape the kill."""
    parent_id = make_queued_task(db_session)
    db_session.get(Task, parent_id).status = "running"
    db_session.commit()
    created = {}

    def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        interrupt_event.wait(timeout=5)
        child = Task(description="late child", status="queued", full_auto=False, parent_task_id=task_id)
        session.add(child)
        session.get(Task, task_id).status = "interrupted"
        session.commit()
        created["child_id"] = child.id
        session.close()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    scheduler._interrupt_events[parent_id] = threading.Event()
    scheduler._running[parent_id] = asyncio.create_task(
        asyncio.to_thread(
            fake_run, parent_id, session_factory(), None, None, scheduler,
            scheduler._interrupt_events[parent_id],
        )
    )
    await scheduler.kill_task(parent_id)

    db_session.expire_all()
    assert db_session.get(Task, created["child_id"]).status == "interrupted"


@pytest.mark.asyncio
async def test_run_one_exception_does_not_clobber_status_already_changed_elsewhere(db_session, session_factory):
    task_id = make_queued_task(db_session)
    db_session.get(Task, task_id).status = "running"
    db_session.commit()

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        other = session_factory()
        other.get(Task, task_id).status = "interrupted"
        other.commit()
        other.close()
        raise RuntimeError("boom")

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    await scheduler._run_one(task_id)

    db_session.expire_all()
    assert db_session.get(Task, task_id).status == "interrupted"


@pytest.mark.asyncio
async def test_run_one_tears_down_sandbox_only_on_terminal_status(db_session, session_factory):
    teardown_calls = []
    fake_sandbox = SimpleNamespace(teardown=lambda task_id: teardown_calls.append(task_id))

    async def fake_run_completes(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(
        session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run_completes, sandbox_manager=fake_sandbox
    )
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert teardown_calls == [task_id]


@pytest.mark.asyncio
async def test_run_one_does_not_tear_down_sandbox_on_pause(db_session, session_factory):
    teardown_calls = []
    fake_sandbox = SimpleNamespace(teardown=lambda task_id: teardown_calls.append(task_id))

    async def fake_run_pauses(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        t = session.get(Task, task_id)
        t.status = "awaiting_answer"
        session.commit()

    scheduler = Scheduler(
        session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run_pauses, sandbox_manager=fake_sandbox
    )
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert teardown_calls == []


@pytest.mark.asyncio
async def test_run_one_forwards_configured_sandbox_manager_to_run_fn(db_session, session_factory):
    """A real run through _run_one must hand run_fn (run_task_loop in
    production) the Scheduler's actual configured sandbox_manager, not
    None -- otherwise sandboxed tools would never see a live manager when
    running through the real _loop_forever -> _run_one path."""
    fake_sandbox = SimpleNamespace(teardown=lambda task_id: None)
    seen = {}

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        seen["sandbox_manager"] = sandbox_manager
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(
        session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run, sandbox_manager=fake_sandbox
    )
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert seen["sandbox_manager"] is fake_sandbox


@pytest.mark.asyncio
async def test_run_one_forwards_configured_browser_service_to_run_fn(db_session, session_factory):
    """A real run through _run_one must hand run_fn (run_task_loop in
    production) the Scheduler's actual configured browser_service, not
    None -- otherwise the headless browser tools would never see a live
    service when running through the real _loop_forever -> _run_one path."""
    fake_browser_service = SimpleNamespace(close_context=lambda task_id: None)
    seen = {}

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        seen["browser_service"] = browser_service
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(
        session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run, browser_service=fake_browser_service
    )
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert seen["browser_service"] is fake_browser_service


@pytest.mark.asyncio
async def test_run_one_still_calls_on_task_terminal_when_teardown_raises(db_session, session_factory):
    """If sandbox_manager.teardown() blows up (e.g. Docker daemon down, or
    the container was already removed), _run_one must still call
    _on_task_terminal -- otherwise a parent task parked in
    "awaiting_subtask" would never get resumed just because teardown
    failed."""

    def raising_teardown(task_id):
        raise RuntimeError("docker daemon unreachable")

    fake_sandbox = SimpleNamespace(teardown=raising_teardown)

    async def fake_run_completes(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(
        session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run_completes, sandbox_manager=fake_sandbox
    )

    parent = Task(description="parent", status="awaiting_subtask", full_auto=False)
    db_session.add(parent)
    db_session.commit()
    child = Task(description="child", status="queued", full_auto=False, parent_task_id=parent.id)
    db_session.add(child)
    db_session.commit()
    child_id = child.id

    # Must not raise despite teardown() blowing up.
    await scheduler._run_one(child_id, threading.Event())

    db_session.expire_all()
    refreshed_parent = db_session.get(Task, parent.id)
    assert refreshed_parent.status == "queued"
    assert refreshed_parent.queue_position is not None


@pytest.mark.asyncio
async def test_run_one_forwards_reviewer_adapter_from_its_own_factory(db_session, session_factory):
    """_run_one must call reviewer_adapter_factory (not just adapter_factory)
    and pass its result through to run_fn as reviewer_adapter, so model
    tiering actually has a distinct reviewer in production."""
    reviewer_sentinel = object()
    seen = {}

    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None, sandbox_manager=None, reviewer_adapter=None, browser_service=None):
        seen["reviewer_adapter"] = reviewer_adapter
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(
        session_factory,
        lambda: None,
        lambda: None,
        cap=1,
        run_fn=fake_run,
        reviewer_adapter_factory=lambda: reviewer_sentinel,
    )
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert seen["reviewer_adapter"] is reviewer_sentinel


def test_mark_waiting_tasks_interrupted_covers_awaiting_sandbox_slot(db_session, session_factory):
    tid = make_queued_task(db_session)
    task = db_session.get(Task, tid)
    task.status = "awaiting_sandbox_slot"
    db_session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3)
    scheduler._mark_waiting_tasks_interrupted(db_session, [tid])

    db_session.expire_all()
    assert db_session.get(Task, tid).status == "interrupted"
