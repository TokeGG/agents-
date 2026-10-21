from app.main import run_startup_recovery
from app.models import Event, Task


def test_startup_recovery_marks_orphaned_running_task_failed(session_factory):
    session = session_factory()
    orphan = Task(description="was running", status="running", full_auto=False)
    session.add(orphan)
    session.commit()
    orphan_id = orphan.id
    session.close()

    run_startup_recovery(session_factory)

    session2 = session_factory()
    refreshed = session2.get(Task, orphan_id)
    assert refreshed.status == "failed"
    gap = session2.query(Event).filter_by(task_id=orphan_id, type="report_gap").one()
    assert "server restarted" in gap.payload["reason"]


def test_startup_recovery_marks_orphaned_awaiting_subtask_task_failed(session_factory):
    session = session_factory()
    orphan = Task(description="was waiting on a subtask", status="awaiting_subtask", full_auto=False)
    session.add(orphan)
    session.commit()
    orphan_id = orphan.id
    session.close()

    run_startup_recovery(session_factory)

    session2 = session_factory()
    refreshed = session2.get(Task, orphan_id)
    assert refreshed.status == "failed"
    gap = session2.query(Event).filter_by(task_id=orphan_id, type="report_gap").one()
    assert "server restarted" in gap.payload["reason"]


def test_startup_recovery_leaves_queued_tasks_alone(session_factory):
    session = session_factory()
    queued = Task(description="still waiting", status="queued", full_auto=False)
    session.add(queued)
    session.commit()
    queued_id = queued.id
    session.close()

    run_startup_recovery(session_factory)

    session2 = session_factory()
    refreshed = session2.get(Task, queued_id)
    assert refreshed.status == "queued"
