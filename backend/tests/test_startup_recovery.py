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


def test_startup_recovery_cascades_to_descendants_of_failed_parent(session_factory):
    session = session_factory()
    parent = Task(description="crashed parent", status="awaiting_subtask", full_auto=False)
    session.add(parent)
    session.commit()
    child = Task(description="queued child", status="queued", full_auto=False, parent_task_id=parent.id)
    session.add(child)
    session.commit()
    grandchild = Task(
        description="waiting grandchild", status="awaiting_answer", full_auto=False, parent_task_id=child.id
    )
    unrelated = Task(description="unrelated queued", status="queued", full_auto=False)
    session.add_all([grandchild, unrelated])
    session.commit()
    ids = (parent.id, child.id, grandchild.id, unrelated.id)
    session.close()

    run_startup_recovery(session_factory)

    s = session_factory()
    assert s.get(Task, ids[0]).status == "failed"
    assert s.get(Task, ids[1]).status == "interrupted"
    assert s.get(Task, ids[2]).status == "interrupted"
    assert s.get(Task, ids[3]).status == "queued"
    gap = s.query(Event).filter_by(task_id=ids[1], type="report_gap").one()
    assert "ancestor" in gap.payload["reason"]
