from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.events import router as events_router
from app.api.kill_switch import router as kill_switch_router
from app.api.tasks import get_adapter, get_registry, router as tasks_router
from app.db import get_session_factory
from app.models import Event, Task
from app.scheduler import WAITING_STATUSES, Scheduler, collect_descendant_ids

RESTART_REPORT_GAP_REASON = (
    "server restarted while this task was running — its in-progress state could not be resumed safely"
)
RESTART_REPORT_GAP_OPTIONS = [
    "retry the task",
    "review its trace for partial side effects before retrying",
]
ORPHANED_DESCENDANT_REASON = (
    "an ancestor task was lost in a server restart, so nothing is left to resume this subtask"
)
ORPHANED_DESCENDANT_OPTIONS = ["retry the top-level task", "start a new task"]


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
        # Cascade: still-queued/waiting descendants of a now-failed task are
        # orphaned (their parent will never resume them), so mark them
        # "interrupted" rather than letting a queued child run for nothing or
        # leaving a waiting grandchild in "needs you" forever.
        for task in orphaned:
            for tid in collect_descendant_ids(session, task.id)[1:]:
                descendant = session.get(Task, tid)
                if descendant is not None and descendant.status in WAITING_STATUSES:
                    descendant.status = "interrupted"
                    session.add(
                        Event(
                            task_id=tid,
                            type="report_gap",
                            payload={
                                "reason": ORPHANED_DESCENDANT_REASON,
                                "options": ORPHANED_DESCENDANT_OPTIONS,
                            },
                        )
                    )
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
app.include_router(kill_switch_router)
app.include_router(dashboard_router)
