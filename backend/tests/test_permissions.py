from app.agent.permissions import needs_approval
from app.models import Task


def test_needs_approval_for_irreversible_when_not_full_auto():
    task = Task(full_auto=False)
    assert needs_approval("send_email", task) is True


def test_no_approval_needed_when_full_auto():
    task = Task(full_auto=True)
    assert needs_approval("send_email", task) is False


def test_no_approval_needed_for_reversible_tool():
    task = Task(full_auto=False)
    assert needs_approval("http_get", task) is False
