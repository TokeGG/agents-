import pytest

from app.api.tasks import get_registry
from app.connectors.registry import (
    BUILTIN_TOOL_NAMES,
    approve,
    create_pending,
    find_active,
    find_pending,
)


def test_builtin_tool_names_matches_get_registry(db_session):
    """BUILTIN_TOOL_NAMES is a hardcoded mirror of what get_registry()
    actually registers; nothing else enforces the two stay in sync. With no
    active connectors, every tool get_registry() registers is a built-in
    (DynamicConnectorTool entries only come from active DB connectors), so
    the two name sets must be identical."""
    registry = get_registry(db_session)
    registered_names = set(registry._tools.keys())
    assert registered_names == BUILTIN_TOOL_NAMES


def test_create_pending_then_find_pending_not_active(db_session, task):
    create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="create issues", parameters_schema={"type": "object"},
        source_code="print('x')", required_hosts=["api.github.com"],
        requested_by_task_id=task.id, test_transcript={"ok": True},
    )
    assert find_pending(db_session, "github") is not None
    assert find_active(db_session, "github", "create issues") is None


def test_approve_sets_active_and_rejects_builtin_name_collision(db_session, task):
    c = create_pending(
        db_session, name="run_shell", service_name="evil", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    with pytest.raises(ValueError):
        approve(db_session, c.id, approved_by="user")

    ok = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="x", parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    approved = approve(db_session, ok.id, approved_by="user")
    assert approved.status == "active"
    assert approved.approved_by == "user"
    assert approved.approved_at is not None


def test_approve_rejects_collision_with_active_connector(db_session, task):
    first = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="a", parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    approve(db_session, first.id, approved_by="user")
    dup = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="b (a different purpose, different row)", parameters_schema={},
        source_code="", required_hosts=[], requested_by_task_id=task.id,
        test_transcript={},
    )
    with pytest.raises(ValueError):
        approve(db_session, dup.id, approved_by="user")
