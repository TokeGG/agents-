"""SandboxManager: per-task container lifecycle, slot cap, and sandboxed tool execution."""

from __future__ import annotations

import threading
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

TERMINAL_STATUSES = {"completed", "failed", "interrupted"}


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
        # Task ids whose container the idle reaper has stopped (but not
        # removed). Their container + volume are kept so the next sandbox
        # call restarts the same container; they do not count toward `cap`.
        self._stopped: set[int] = set()
        # Guards all bookkeeping above (_slots, _waiting, _volume_names,
        # _paused_since, _stopped). SandboxManager is called from several
        # asyncio.to_thread workers at once, so the cap check-then-insert must
        # be atomic. Slow runtime calls (Docker launch/stop/exec) happen
        # outside the lock.
        self._lock = threading.Lock()

    def _running_count_locked(self) -> int:
        return len(self._slots) - len(self._stopped)

    def _enqueue_wait_locked(self, task_id: int) -> None:
        if task_id not in self._waiting:
            self._waiting.append(task_id)

    def get_or_create_container(self, task_id: int) -> str:
        with self._lock:
            if task_id in self._slots:
                container_id = self._slots[task_id]
                if task_id not in self._stopped:
                    return container_id
                # Reaped while idle: restart the same container (its volume
                # holds the auto-committed state), subject to the cap.
                if self._running_count_locked() >= self.cap:
                    self._enqueue_wait_locked(task_id)
                    raise SandboxSlotUnavailable(f"no sandbox slot available for task {task_id}")
                self._stopped.discard(task_id)
                restart = True
            else:
                if self._running_count_locked() >= self.cap:
                    self._enqueue_wait_locked(task_id)
                    raise SandboxSlotUnavailable(f"no sandbox slot available for task {task_id}")
                volume_name = f"sandbox-task-{task_id}-{uuid.uuid4().hex[:8]}"
                container_id = f"sandbox-{task_id}-{uuid.uuid4().hex[:8]}"
                # Reserve the slot before releasing the lock so concurrent
                # callers see it when checking the cap.
                self._slots[task_id] = container_id
                self._volume_names[task_id] = volume_name
                restart = False

        if restart:
            try:
                self.runtime.restart_container(container_id)
            except Exception:
                with self._lock:
                    if self._slots.get(task_id) == container_id:
                        self._stopped.add(task_id)
                raise
            return container_id

        try:
            self.runtime.create_volume(volume_name)
            self.runtime.start_container(
                container_id,
                volume_name,
                self.image,
                self.memory_limit,
                self.cpu_limit,
                "sandbox-net",
            )
        except Exception:
            with self._lock:
                if self._slots.get(task_id) == container_id:
                    self._slots.pop(task_id, None)
                    self._volume_names.pop(task_id, None)
            raise
        return container_id

    def remove_waiting(self, task_id: int) -> None:
        """Drop task_id from the slot wait queue (e.g. it was killed while
        parked in awaiting_sandbox_slot), so teardown never resurrects it."""
        with self._lock:
            if task_id in self._waiting:
                self._waiting.remove(task_id)

    def teardown(self, task_id: int) -> None:
        with self._lock:
            container_id = self._slots.pop(task_id, None)
            volume_name = self._volume_names.pop(task_id, None)
            self._paused_since.pop(task_id, None)
            self._stopped.discard(task_id)
            if task_id in self._waiting:
                self._waiting.remove(task_id)

        if container_id is not None:
            self.runtime.stop_container(container_id)
            self.runtime.remove_container(container_id)
        if volume_name is not None:
            self.runtime.remove_volume(volume_name)

        self._wake_next_waiter()

    def _wake_next_waiter(self) -> None:
        """Re-enqueue (front of queue) the next task waiting for a slot,
        skipping any whose task has already reached a terminal status (e.g.
        killed while waiting) so it is never resurrected."""
        if self.scheduler is None:
            with self._lock:
                if self._waiting:
                    self._waiting.pop(0)
            return
        session = self.session_factory()
        try:
            while True:
                with self._lock:
                    if not self._waiting:
                        return
                    waiting_task_id = self._waiting.pop(0)
                if session is not None:
                    task = session.get(Task, waiting_task_id)
                    if task is None or task.status in TERMINAL_STATUSES:
                        continue
                self.scheduler.enqueue(session, waiting_task_id, front=True)
                return
        finally:
            if session is not None and hasattr(session, "close"):
                session.close()

    def _volume_name(self, task_id: int) -> str | None:
        return self._volume_names.get(task_id)

    def reap_idle(self, session) -> None:
        """Auto-commits and stops (but does not remove) containers for tasks that
        have been sitting in a waiting status longer than `idle_timeout_seconds`.
        A reaped container frees its slot; the next sandbox call for that task
        restarts the same container (see get_or_create_container)."""
        now = time.monotonic()
        with self._lock:
            candidates = [tid for tid in self._slots if tid not in self._stopped]
        for task_id in candidates:
            task = session.get(Task, task_id)
            status = task.status if task is not None else None

            with self._lock:
                if status not in WAITING_STATUSES:
                    self._paused_since.pop(task_id, None)
                    continue
                paused_at = self._paused_since.setdefault(task_id, now)
                if now - paused_at < self.idle_timeout_seconds:
                    continue
                container_id = self._slots.get(task_id)
                if container_id is None or task_id in self._stopped:
                    continue

            self.run_shell(task_id, "git add -A && git commit -m 'auto-commit before idle reap' || true")
            self.runtime.stop_container(container_id)
            with self._lock:
                self._paused_since.pop(task_id, None)
                if self._slots.get(task_id) == container_id:
                    self._stopped.add(task_id)
            self._wake_next_waiter()

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
