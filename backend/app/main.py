from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.events import router as events_router
from app.api.tasks import get_adapter, get_registry, router as tasks_router
from app.db import get_session_factory
from app.models import Event, Task
from app.scheduler import Scheduler

RESTART_REPORT_GAP_REASON = (
    "server restarted while this task was running — its in-progress state could not be resumed safely"
)
RESTART_REPORT_GAP_OPTIONS = [
    "retry the task",
    "review its trace for partial side effects before retrying",
]


def run_startup_recovery(session_factory) -> None:
    """Marks tasks orphaned by a crash/restart (left in "running" or
    "awaiting_subtask" with no process left to run them) as "failed", with a
    report_gap event explaining why. Must run before the Scheduler starts
    popping the queue."""
    session = session_factory()
    try:
        orphaned = session.query(Task).filter(Task.status.in_(["running", "awaiting_subtask"])).all()
        for task in orphaned:
            session.add(
                Event(
                    task_id=task.id,
                    type="report_gap",
                    payload={
                        "reason": RESTART_REPORT_GAP_REASON,
                        "options": RESTART_REPORT_GAP_OPTIONS,
                    },
                )
            )
            task.status = "failed"
        session.commit()
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = Scheduler(
        session_factory=lambda: app.dependency_overrides.get(get_session_factory, get_session_factory)()(),
        adapter_factory=lambda: app.dependency_overrides.get(get_adapter, get_adapter)(),
        registry_factory=lambda: app.dependency_overrides.get(get_registry, get_registry)(),
    )
    app.state.scheduler = scheduler
    run_startup_recovery(scheduler.session_factory)
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(tasks_router)
app.include_router(events_router)
