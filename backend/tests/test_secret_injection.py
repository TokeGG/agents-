from app.connectors.secret_injection import provision_secret_file, redact_secret
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime


def test_provision_secret_file_writes_and_returns_path(tmp_path, db_session, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)
    path = provision_secret_file(manager, task.id, "s3cr3t-value")
    assert path.startswith("secrets/")
    result = manager.read_file(task.id, path)
    assert result["output"].strip() == "s3cr3t-value"


def test_redact_secret_replaces_every_occurrence_and_handles_none():
    text = "error: token s3cr3t-value rejected (raw: s3cr3t-value)"
    assert redact_secret(text, "s3cr3t-value") == (
        "error: token ***REDACTED*** rejected (raw: ***REDACTED***)"
    )
    assert redact_secret(text, None) == text
    assert redact_secret(text, "") == text
