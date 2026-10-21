import threading

from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.models import Base, Task


def test_default_sqlite_memory_engine_is_shared_across_threads():
    """The unconfigured default DATABASE_URL (sqlite in-memory) must give the
    Scheduler's worker threads the same database the main thread created,
    not a separate empty per-thread one."""
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    t = Task(description="from main thread", status="queued", full_auto=False)
    s.add(t)
    s.commit()
    task_id = t.id
    s.close()

    seen = {}

    def worker():
        try:
            ws = factory()
            seen["description"] = ws.get(Task, task_id).description
            ws.close()
        except Exception as exc:  # noqa: BLE001
            seen["error"] = repr(exc)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=5)
    assert seen.get("description") == "from main thread", seen
