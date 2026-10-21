import asyncio
import threading
from typing import Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Task

POLL_INTERVAL_SECONDS = 0.05


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
        self._lock = threading.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

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

    async def _loop_forever(self) -> None:
        while True:
            try:
                while len(self._running) < self._cap:
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
        finally:
            session.close()
            self._running.pop(task_id, None)
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
