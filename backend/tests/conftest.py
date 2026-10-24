from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Task


def _shared_sqlite_engine():
    """An in-memory SQLite engine on a single StaticPool connection, shared
    between the request-handling thread(s) and the Scheduler's background
    worker thread(s), so they see the same data."""
    return create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


@pytest.fixture(autouse=True)
def _default_sandbox_egress_allowlist(monkeypatch):
    """The app now fails closed at startup (lifespan) when
    SANDBOX_EGRESS_ALLOWLIST is unset/empty. Default it here so existing
    tests that spin up a TestClient (triggering lifespan) don't all need to
    set it themselves; a test that wants to exercise the unset/empty case
    uses monkeypatch.delenv/setenv itself to override this default."""
    monkeypatch.setenv("SANDBOX_EGRESS_ALLOWLIST", "api.example.com")


@pytest.fixture(autouse=True)
def _default_vault_encryption_key(monkeypatch):
    """The app now fails closed at startup (lifespan) when
    VAULT_ENCRYPTION_KEY is unset. Default it here so existing tests that
    spin up a TestClient (triggering lifespan) don't all need to set it
    themselves; a test that wants to exercise the unset case uses
    monkeypatch.delenv itself to override this default."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


TEST_AUTH_TOKEN = "test-auth-token"


@pytest.fixture()
def client(monkeypatch, session_factory):
    """A TestClient wired to a shared in-memory SQLite DB (StaticPool, so the
    request session and any background-task session see the same data), a
    FakeModelAdapter the test can configure per-call via dependency overrides,
    and a default Authorization header so existing tests don't need to know
    about auth. Tests that exercise auth itself build their own TestClient
    (see test_auth.py) instead of relying on this default header.

    Uses the `session_factory` fixture's engine (rather than a private one)
    so a test that also takes `db_session` sees the same rows the API reads
    and writes -- `db_session` is built from this same fixture."""
    from app.agent.model_adapter import FakeModelAdapter
    from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool
    from app.api import tasks as tasks_module
    from app.db import get_session, get_session_factory
    from app.main import app

    monkeypatch.setenv("AUTH_TOKEN", TEST_AUTH_TOKEN)

    TestSessionLocal = session_factory

    def override_get_session():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    def override_get_session_factory():
        return TestSessionLocal

    def override_get_registry(session=None):
        registry = ToolRegistry()
        registry.register(http_get_tool)
        registry.register(ask_user_tool)
        registry.register(report_gap_tool)
        return registry

    def override_get_sandbox_manager(session_factory):
        import tempfile

        from app.sandbox.manager import SandboxManager
        from app.sandbox.runtime import FakeContainerRuntime

        base_dir = Path(tempfile.mkdtemp(prefix="sandbox-test-"))
        return SandboxManager(FakeContainerRuntime(base_dir), session_factory=session_factory)

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.dependency_overrides[tasks_module.get_registry] = override_get_registry
    app.dependency_overrides[tasks_module.get_sandbox_manager] = override_get_sandbox_manager
    # Default adapter: immediately finishes. Tests override this per-case via
    # app.dependency_overrides[tasks_module.get_adapter] for other scripts.
    app.dependency_overrides[tasks_module.get_adapter] = lambda: FakeModelAdapter(
        script=[{"type": "final_answer", "content": "done"}]
    )

    with TestClient(app, headers={"Authorization": f"Bearer {TEST_AUTH_TOKEN}"}) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def session_factory():
    engine = _shared_sqlite_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def task(db_session):
    t = Task(description="test task", status="running", full_auto=False)
    db_session.add(t)
    db_session.commit()
    return t
