"""SandboxManager: per-task container lifecycle, slot cap, and sandboxed tool execution."""

from __future__ import annotations

import time
import uuid

from app.models import Task
from app.sandbox.runtime import ContainerRuntime, SandboxSlotUnavailable

WAITING_STATUSES = {
    "awaiting_answer",
    "awaiting_approval",
    "awaiting_subtask",
    "awaiting_sandbox_slot",
}


class SandboxManager:
    """Owns one container per task, up to `cap` concurrent slots.

    Containers are created lazily on first use (`get_or_create_container`) and torn
    down via `teardown`, which frees the slot and (if a scheduler was provided)
    re-enqueues the next waiting task at the front of the queue.
    """

    def __init__(
        self,
        runtime: ContainerRuntime,
        session_factory,
        scheduler=None,
        cap: int = 2,
        image: str = "agents-sandbox:latest",
        memory_limit: str = "2g",
        cpu_limit: float = 1.0,
        idle_timeout_seconds: int = 14400,
    ):
        self.runtime = runtime
        self.session_factory = session_factory
        self.scheduler = scheduler
        self.cap = cap
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.idle_timeout_seconds = idle_timeout_seconds

        self._slots: dict[int, str] = {}
        self._waiting: list[int] = []
        self._volume_names: dict[int, str] = {}
        self._paused_since: dict[int, float] = {}

    def get_or_create_container(self, task_id: int) -> str:
        if task_id in self._slots:
            return self._slots[task_id]

        if len(self._slots) >= self.cap:
            if task_id not in self._waiting:
                self._waiting.append(task_id)
            raise SandboxSlotUnavailable(f"no sandbox slot available for task {task_id}")

        volume_name = f"sandbox-task-{task_id}-{uuid.uuid4().hex[:8]}"
        container_id = f"sandbox-{task_id}-{uuid.uuid4().hex[:8]}"

        self.runtime.create_volume(volume_name)
        self.runtime.start_container(
            container_id,
            volume_name,
            self.image,
            self.memory_limit,
            self.cpu_limit,
            "sandbox-net",
        )

        self._slots[task_id] = container_id
        self._volume_names[task_id] = volume_name
        return container_id

    def teardown(self, task_id: int) -> None:
        container_id = self._slots.pop(task_id, None)
        volume_name = self._volume_names.pop(task_id, None)

        if container_id is not None:
            self.runtime.stop_container(container_id)
            self.runtime.remove_container(container_id)
        if volume_name is not None:
            self.runtime.remove_volume(volume_name)

        if self._waiting:
            waiting_task_id = self._waiting.pop(0)
            if self.scheduler is not None:
                session = self.session_factory()
                try:
                    self.scheduler.enqueue(session, waiting_task_id, front=True)
                finally:
                    if session is not None and hasattr(session, "close"):
                        session.close()

    def _volume_name(self, task_id: int) -> str | None:
        return self._volume_names.get(task_id)

    def reap_idle(self, session) -> None:
        """Auto-commits and stops (but does not remove) containers for tasks that
        have been sitting in a waiting status longer than `idle_timeout_seconds`."""
        now = time.monotonic()
        for task_id in list(self._slots):
            task = session.get(Task, task_id)
            status = task.status if task is not None else None

            if status not in WAITING_STATUSES:
                self._paused_since.pop(task_id, None)
                continue

            paused_at = self._paused_since.setdefault(task_id, now)
            if now - paused_at < self.idle_timeout_seconds:
                continue

            container_id = self._slots.get(task_id)
            if container_id is None:
                continue

            self.run_shell(task_id, "git add -A && git commit -m 'auto-commit before idle reap' || true")
            self.runtime.stop_container(container_id)

    def run_shell(self, task_id: int, command: str, timeout: int = 120) -> dict:
        container_id = self.get_or_create_container(task_id)
        exit_code, output = self.runtime.exec(container_id, command, timeout=timeout)
        return {"ok": exit_code == 0, "output": output, "data": None}

    def read_file(self, task_id: int, path: str) -> dict:
        return self.run_shell(task_id, f"cat {_quote(path)}")

    def write_file(self, task_id: int, path: str, content: str) -> dict:
        marker = f"__EOF_{uuid.uuid4().hex}__"
        command = f"cat > {_quote(path)} << '{marker}'\n{content}\n{marker}\n"
        return self.run_shell(task_id, command)

    def edit_file(self, task_id: int, path: str, old: str, new: str) -> dict:
        script = (
            "import pathlib, sys\n"
            f"p = pathlib.Path({path!r})\n"
            "text = p.read_text()\n"
            f"old = {old!r}\n"
            f"new = {new!r}\n"
            "if old not in text:\n"
            "    print('old text not found', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "p.write_text(text.replace(old, new))\n"
        )
        command = f"python3 -c {_quote(script)}"
        return self.run_shell(task_id, command)


def _quote(value: str) -> str:
    """Shell-quote a string for safe inclusion in a `sh -c`-style command."""
    return "'" + value.replace("'", "'\\''") + "'"
