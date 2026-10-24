import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.events import router as events_router
from app.api.kill_switch import router as kill_switch_router
from app.api.tasks import (
    get_adapter,
    get_browser_service,
    get_registry,
    get_reviewer_adapter,
    get_sandbox_manager,
    router as tasks_router,
)
from app.db import get_session_factory
from app.models import Event, Task
from app.sandbox.runtime import validate_sandbox_egress_config
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


REAPER_POLL_INTERVAL_SECONDS = 60


async def _run_idle_reaper(sandbox_manager, session_factory) -> None:
    while True:
        await asyncio.sleep(REAPER_POLL_INTERVAL_SECONDS)
        session = session_factory()
        try:
            sandbox_manager.reap_idle(session)
        finally:
            if session is not None and hasattr(session, "close"):
                session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_sandbox_egress_config()
    session_factory = lambda: app.dependency_overrides.get(get_session_factory, get_session_factory)()()
    sandbox_manager = app.dependency_overrides.get(get_sandbox_manager, get_sandbox_manager)(session_factory)
    browser_service = app.dependency_overrides.get(get_browser_service, get_browser_service)()
    scheduler = Scheduler(
        session_factory=session_factory,
        adapter_factory=lambda: app.dependency_overrides.get(get_adapter, get_adapter)(),
        registry_factory=lambda: app.dependency_overrides.get(get_registry, get_registry)(),
        reviewer_adapter_factory=lambda: app.dependency_overrides.get(
            get_reviewer_adapter, get_reviewer_adapter
        )(),
        sandbox_manager=sandbox_manager,
        browser_service=browser_service,
    )
    sandbox_manager.scheduler = scheduler
    app.state.scheduler = scheduler
    run_startup_recovery(scheduler.session_factory)
    await scheduler.start()
    reaper_task = asyncio.create_task(_run_idle_reaper(sandbox_manager, session_factory))
    yield
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    await scheduler.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(tasks_router)
app.include_router(events_router)
app.include_router(kill_switch_router)
app.include_router(dashboard_router)
