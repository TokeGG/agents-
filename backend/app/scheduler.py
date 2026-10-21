import asyncio
import threading
from typing import Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Event, Task

POLL_INTERVAL_SECONDS = 0.05
# How long stop() waits for in-flight task runs to notice their
# interrupt_event and finish, before giving up on them (best effort: the
# underlying asyncio.to_thread worker thread cannot be forced to stop).
STOP_DRAIN_TIMEOUT_SECONDS = 5.0


class Scheduler:
    """Bounded-concurrency task scheduler with urgent-first queue ordering.

    Kill-switch and delegation-aware termination handling are out of scope
    for this task; `_on_task_terminal` is a no-op stub here, filled in later.
    """

    def __init__(
        self,
        session_factory,
        adapter_factory: Callable,
        registry_factory: Callable,
        cap: int = 3,
        run_fn: Optional[Callable] = None,
    ):
        self.session_factory = session_factory
        self.adapter_factory = adapter_factory
        self.registry_factory = registry_factory
        self._cap = cap
        if run_fn is None:
            from app.agent.loop import run_task_loop

            run_fn = run_task_loop
        self.run_fn = run_fn

        self._running: dict[int, asyncio.Task] = {}
        self._interrupt_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._mode = "normal"

    async def start(self) -> None:
        self._stopped.clear()
        self._loop_task = asyncio.create_task(self._loop_forever())

    async def stop(self) -> None:
        self._stopped.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        # Also ask any in-flight task runs to stop: otherwise they keep
        # running in background threads (asyncio.to_thread) past this call,
        # which in tests leaks a stale run into the next test's app/dependency
        # state. Cancelling the asyncio.Task here would NOT work: it only
        # cancels the coroutine awaiting asyncio.to_thread, not the real OS
        # thread running run_fn (to_thread cannot be interrupted), and it
        # would make _run_one's `finally` close/pop the session while that
        # thread is still using it (cross-thread Session misuse). Instead,
        # signal each run's interrupt_event (run_task_loop checks it between
        # steps and returns) and wait for the real completion, up to a bound.
        running = list(self._running.values())
        for interrupt_event in self._interrupt_events.values():
            interrupt_event.set()
        if running:
            await asyncio.wait(running, timeout=STOP_DRAIN_TIMEOUT_SECONDS)
        # Tasks that finished drop themselves from _running/_interrupt_events
        # in _run_one's own finally; anything still present here didn't
        # honor the interrupt in time and is left running in the background
        # (best effort only - its thread cannot be forced to stop).

    def status(self) -> dict:
        return {"mode": self._mode, "running": len(self._running), "cap": self._cap}

    async def kill_switch(self, mode: str) -> dict:
        """Sets the scheduler's mode. "pause" stops new tasks from being
        popped off the queue (in-flight tasks finish normally). "stop" does
        the same, and additionally signals every currently-running task's
        interrupt_event so they wind down cooperatively. "normal" resumes
        popping."""
        self._mode = mode
        if mode == "stop":
            self._collect_and_interrupt(list(self._interrupt_events.keys()))
        return self.status()

    async def kill_task(self, task_id: int) -> None:
        """Interrupts task_id and every descendant task (walking
        parent_task_id), for whichever of them are currently running (i.e.
        present in self._interrupt_events). The loop's own cooperative check
        (checked between steps in run_task_loop) does the rest: marks the
        task "interrupted", writes a report_gap event, and returns."""
        session = self.session_factory()
        try:
            ids = self._collect_descendant_ids(session, task_id)
        finally:
            session.close()
        self._collect_and_interrupt(ids)

    def _collect_descendant_ids(self, session: Session, task_id: int) -> list[int]:
        ids = [task_id]
        frontier = [task_id]
        while frontier:
            children = (
                session.query(Task.id)
                .filter(Task.parent_task_id.in_(frontier))
                .all()
            )
            child_ids = [c.id for c in children]
            frontier = [cid for cid in child_ids if cid not in ids]
            ids.extend(frontier)
        return ids

    def _collect_and_interrupt(self, ids) -> None:
        for task_id in ids:
            interrupt_event = self._interrupt_events.get(task_id)
            if interrupt_event is not None:
                interrupt_event.set()

    async def _loop_forever(self) -> None:
        while True:
            try:
                while self._mode == "normal" and len(self._running) < self._cap:
                    session = self.session_factory()
                    try:
                        task = self._pop_next(session)
                        if task is not None:
                            task_id = task.id
                            task.status = "running"
                            session.commit()
                    finally:
                        session.close()
                    if task is None:
                        break
                    self._running[task_id] = asyncio.create_task(self._run_one(task_id))
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise

    def _pop_next(self, session: Session) -> Optional[Task]:
        urgent = (
            session.query(Task)
            .filter(Task.status == "queued", Task.urgent.is_(True))
            .order_by(Task.queue_position.asc())
            .first()
        )
        if urgent is not None:
            return urgent
        return (
            session.query(Task)
            .filter(Task.status == "queued")
            .order_by(Task.queue_position.asc())
            .first()
        )

    async def _run_one(self, task_id: int) -> None:
        interrupt_event = threading.Event()
        self._interrupt_events[task_id] = interrupt_event
        session = self.session_factory()
        try:
            args = (
                task_id,
                session,
                self.adapter_factory(),
                self.registry_factory(),
                self,
                interrupt_event,
            )
            if asyncio.iscoroutinefunction(self.run_fn):
                # Test doubles may be async; real run_fn (run_task_loop) is
                # sync and blocking, so it runs in a worker thread below.
                await self.run_fn(*args)
            else:
                await asyncio.to_thread(self.run_fn, *args)
        except Exception as exc:  # noqa: BLE001 - must never leave a task stuck "running" silently
            session.rollback()
            task = session.get(Task, task_id)
            if task is not None:
                session.add(
                    Event(
                        task_id=task_id,
                        type="report_gap",
                        payload={
                            "reason": f"internal error while running the task: {exc}",
                            "options": [
                                "retry the task",
                                "check the model/API configuration",
                                "contact support with the task id",
                            ],
                        },
                    )
                )
                task.status = "failed"
                session.commit()
        finally:
            session.close()
            self._running.pop(task_id, None)
            self._interrupt_events.pop(task_id, None)
            self._on_task_terminal(task_id)

    def _on_task_terminal(self, task_id: int) -> None:
        """No-op stub for this task. Delegation-aware parent handling is
        added in a later task."""
        pass

    def enqueue(self, session: Session, task_id: int, front: bool = False) -> None:
        with self._lock:
            task = session.get(Task, task_id)
            if task is None:
                return
            if front:
                min_pos = session.query(func.min(Task.queue_position)).filter(
                    Task.status == "queued"
                ).scalar()
                new_pos = (min_pos if min_pos is not None else 0) - 1
            else:
                max_pos = session.query(func.max(Task.queue_position)).filter(
                    Task.status == "queued"
                ).scalar()
                new_pos = (max_pos if max_pos is not None else -1) + 1
            task.status = "queued"
            task.queue_position = new_pos
            session.commit()
