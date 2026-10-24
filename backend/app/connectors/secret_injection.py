"""Shared helpers for handling credentials inside sandbox containers."""

import uuid

REDACTION_MARKER = "***REDACTED***"


def provision_secret_file(sandbox_manager, task_id: int, secret_value: str) -> str:
    """Write a secret into the task's sandbox and return its path under secrets/.

    The path is relative (to the sandbox volume's working directory), not
    absolute: FakeContainerRuntime.exec runs real subprocesses with
    cwd=volume_dir, but an absolute path in a shell command resolves
    against the real filesystem root regardless of cwd, which would write
    (and later fail to find, for shredding checks) outside the per-task
    test volume entirely. A relative path keeps everything inside the
    task's own sandbox volume on both the fake and real runtimes.
    """
    path = f"secrets/{uuid.uuid4().hex}.token"
    # write_file does not create parent directories, so ensure secrets/ exists first.
    mkdir = sandbox_manager.run_shell(task_id, "mkdir -p -m 700 secrets")
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
