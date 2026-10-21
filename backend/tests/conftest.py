import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Task


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    """A TestClient wired to a shared in-memory SQLite DB (StaticPool, so the
    request session and any background-task session see the same data) and a
    FakeModelAdapter the test can configure per-call via dependency overrides."""
    from app.agent.model_adapter import FakeModelAdapter
    from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool
    from app.api import tasks as tasks_module
    from app.db import get_session, get_session_factory
    from app.main import app

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine)

    def override_get_session():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    def override_get_session_factory():
        return TestSessionLocal

    def override_get_registry():
        registry = ToolRegistry()
        registry.register(http_get_tool)
        registry.register(ask_user_tool)
        registry.register(report_gap_tool)
        return registry

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.dependency_overrides[tasks_module.get_registry] = override_get_registry
    # Default adapter: immediately finishes. Tests override this per-case via
    # app.dependency_overrides[tasks_module.get_adapter] for other scripts.
    app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "final_answer", "content": "done"}]
    )

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def task(db_session):
    t = Task(description="test task", status="running", full_auto=False)
    db_session.add(t)
    db_session.commit()
    return t
