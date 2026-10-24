"""Shared helpers for handling credentials inside sandbox containers."""

import uuid

REDACTION_MARKER = "***REDACTED***"


def provision_secret_file(sandbox_manager, task_id: int, secret_value: str) -> str:
    """Write a secret into the task's sandbox and return its path under /secrets/."""
    path = f"/secrets/{uuid.uuid4().hex}.token"
    # write_file does not create parent directories, so ensure /secrets exists first.
    mkdir = sandbox_manager.run_shell(task_id, "mkdir -p -m 700 /secrets")
    if not mkdir["ok"]:
        raise RuntimeError(mkdir["output"])
    result = sandbox_manager.write_file(task_id, path, secret_value)
    if not result["ok"]:
        raise RuntimeError(result["output"])
    return path


def redact_secret(text: str, secret_value: str | None) -> str:
    """Replace every literal occurrence of secret_value in text."""
    if not secret_value:
        return text
    return text.replace(secret_value, REDACTION_MARKER)
