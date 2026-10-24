from types import SimpleNamespace

import pytest

from app.models import Task
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime, SandboxSlotUnavailable


def test_get_or_create_reuses_existing_container(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    first = manager.get_or_create_container(task_id=1)
    second = manager.get_or_create_container(task_id=1)
    assert first == second


def test_third_task_raises_slot_unavailable_at_cap(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    manager.get_or_create_container(task_id=2)
    with pytest.raises(SandboxSlotUnavailable):
        manager.get_or_create_container(task_id=3)


def test_teardown_frees_slot_and_reenqueues_waiting_task(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    enqueued = []
    fake_scheduler = SimpleNamespace(enqueue=lambda session, task_id, front: enqueued.append((task_id, front)))
    manager = SandboxManager(runtime, session_factory=lambda: None, scheduler=fake_scheduler, cap=1)
    manager.get_or_create_container(task_id=1)
    with pytest.raises(SandboxSlotUnavailable):
        manager.get_or_create_container(task_id=2)
    manager.teardown(task_id=1)
    assert enqueued == [(2, True)]


def test_run_shell_executes_command_in_task_container(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    result = manager.run_shell(task_id=1, command="echo hi")
    assert result["ok"] is True
    assert "hi" in result["output"]
    assert result["data"] is None


def test_run_shell_command_timeout_returns_failed_result_not_exception(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    result = manager.run_shell(task_id=1, command="sleep 5", timeout=1)
    assert result == {"ok": False, "output": "command timed out after 1s", "data": None}


def test_write_then_read_file_round_trip(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    write_result = manager.write_file(task_id=1, path="hello.txt", content="hello world")
    assert write_result["ok"] is True

    read_result = manager.read_file(task_id=1, path="hello.txt")
    assert read_result["ok"] is True
    assert "hello world" in read_result["output"]


def test_edit_file_replaces_content(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    manager.write_file(task_id=1, path="hello.txt", content="hello world")

    edit_result = manager.edit_file(task_id=1, path="hello.txt", old="world", new="there")
    assert edit_result["ok"] is True

    read_result = manager.read_file(task_id=1, path="hello.txt")
    assert "hello there" in read_result["output"]
    assert "world" not in read_result["output"]


def test_reap_idle_commits_and_stops_long_paused_task(tmp_path, db_session):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=2, idle_timeout_seconds=0)
    task = Task(description="x", status="awaiting_answer", full_auto=False)
    db_session.add(task); db_session.commit()
    container_id = manager.get_or_create_container(task.id)
    (tmp_path / manager._volume_name(task.id) / "file.txt").write_text("hello")
    manager.reap_idle(db_session)
    assert runtime.is_stopped(container_id)
    # committed, not lost: a fresh container on the same volume still has it
    assert (tmp_path / manager._volume_name(task.id) / "file.txt").exists()


def test_reap_idle_does_not_reap_recently_paused_task(tmp_path, db_session):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=2, idle_timeout_seconds=10_000)
    task = Task(description="x", status="awaiting_answer", full_auto=False)
    db_session.add(task); db_session.commit()
    container_id = manager.get_or_create_container(task.id)
    (tmp_path / manager._volume_name(task.id) / "file.txt").write_text("hello")
    manager.reap_idle(db_session)
    assert runtime.is_stopped(container_id) is False


def test_teardown_skips_waiting_task_that_is_already_terminal(tmp_path, db_session):
    enqueued = []
    fake_scheduler = SimpleNamespace(enqueue=lambda session, task_id, front: enqueued.append(task_id))
    manager = SandboxManager(
        FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, scheduler=fake_scheduler, cap=1
    )
    a = Task(description="a", status="running", full_auto=False)
    dead = Task(description="dead", status="awaiting_sandbox_slot", full_auto=False)
    live = Task(description="live", status="awaiting_sandbox_slot", full_auto=False)
    db_session.add_all([a, dead, live]); db_session.commit()
    manager.get_or_create_container(a.id)
    for t in (dead, live):
        with pytest.raises(SandboxSlotUnavailable):
            manager.get_or_create_container(t.id)
    dead.status = "interrupted"; db_session.commit()
    manager.teardown(a.id)
    assert enqueued == [live.id]


def test_fake_runtime_exec_fails_on_stopped_container(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=1)
    cid = manager.get_or_create_container(1)
    runtime.stop_container(cid)
    with pytest.raises(RuntimeError):
        runtime.exec(cid, "true", timeout=5)


def test_reaped_container_is_restarted_and_usable(tmp_path, db_session):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=1, idle_timeout_seconds=0)
    task = Task(description="x", status="awaiting_answer", full_auto=False)
    db_session.add(task); db_session.commit()
    cid = manager.get_or_create_container(task.id)
    manager.write_file(task.id, "f.txt", "kept")
    manager.reap_idle(db_session)
    assert runtime.is_stopped(cid)
    # a second sweep must not touch the already-stopped container
    manager.reap_idle(db_session)
    task.status = "running"; db_session.commit()
    assert manager.get_or_create_container(task.id) == cid
    assert runtime.is_stopped(cid) is False
    result = manager.read_file(task.id, "f.txt")
    assert result["ok"] and "kept" in result["output"]


def test_reaped_container_frees_slot_and_restart_respects_cap(tmp_path, db_session):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=1, idle_timeout_seconds=0)
    a = Task(description="a", status="awaiting_answer", full_auto=False)
    b = Task(description="b", status="running", full_auto=False)
    db_session.add_all([a, b]); db_session.commit()
    manager.get_or_create_container(a.id)
    manager.reap_idle(db_session)
    manager.get_or_create_container(b.id)  # slot freed by reap
    with pytest.raises(SandboxSlotUnavailable):
        manager.get_or_create_container(a.id)
    assert a.id in manager._waiting


def test_cap_respected_under_concurrent_get_or_create(tmp_path):
    import threading
    import time as _time

    class SlowRuntime(FakeContainerRuntime):
        def __init__(self, base_dir):
            super().__init__(base_dir)
            self.live = 0
            self.max_live = 0
            self._m = threading.Lock()

        def start_container(self, *args, **kwargs):
            with self._m:
                self.live += 1
                self.max_live = max(self.max_live, self.live)
            _time.sleep(0.05)
            return super().start_container(*args, **kwargs)

    runtime = SlowRuntime(tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    barrier = threading.Barrier(8)
    results = {}

    def worker(tid):
        barrier.wait()
        try:
            manager.get_or_create_container(tid)
            results[tid] = "ok"
        except SandboxSlotUnavailable:
            results[tid] = "wait"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert list(results.values()).count("ok") == 2
    assert len(manager._slots) == 2
    assert runtime.max_live == 2
    assert sorted(manager._waiting) == sorted(t for t, r in results.items() if r == "wait")
