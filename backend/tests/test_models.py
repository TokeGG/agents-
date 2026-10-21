from app.models import Task, Event, Rating


def test_task_event_rating_roundtrip(db_session):
    task = Task(description="say hello", status="running", full_auto=False)
    db_session.add(task)
    db_session.commit()
    event = Event(task_id=task.id, type="model_step", payload={"note": "started"})
    rating = Rating(task_id=task.id, value=1)
    db_session.add_all([event, rating])
    db_session.commit()
    assert db_session.get(Task, task.id).status == "running"
    assert db_session.query(Event).filter_by(task_id=task.id).count() == 1
    assert db_session.query(Rating).filter_by(task_id=task.id).first().value == 1
