import asyncio
import threading

import pytest

import app.main as main
from app.sandbox.runtime import build_timeout_command


@pytest.mark.asyncio
async def test_idle_reaper_survives_failing_sweep_and_runs_off_loop(monkeypatch):
    monkeypatch.setattr(main, "REAPER_POLL_INTERVAL_SECONDS", 0)
    calls = []
    loop_thread = threading.get_ident()

    class Manager:
        def reap_idle(self, session):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                raise RuntimeError("container already stopped")

    task = asyncio.create_task(main._run_idle_reaper(Manager(), lambda: None))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3
    assert all(t != loop_thread for t in calls)


def test_docker_exec_wraps_command_with_timeout():
    assert build_timeout_command("echo 'hi'", 120) == ["timeout", "-k", "5", "120s", "sh", "-c", "echo 'hi'"]


def test_docker_runtime_exec_applies_timeout_and_maps_timeout_exit():
    from types import SimpleNamespace

    from app.sandbox.runtime import DockerContainerRuntime

    seen = []

    class Container:
        def __init__(self, code):
            self.code = code

        def exec_run(self, cmd, demux=False):
            seen.append(cmd)
            return SimpleNamespace(exit_code=self.code, output=b"out")

    rt = DockerContainerRuntime.__new__(DockerContainerRuntime)
    rt._client = SimpleNamespace(containers=SimpleNamespace(get=lambda cid: Container(124)))
    assert rt.exec("c", "sleep 999", timeout=2) == (-1, "command timed out after 2s")
    assert seen[0] == ["timeout", "-k", "5", "2s", "sh", "-c", "sleep 999"]
    rt._client = SimpleNamespace(containers=SimpleNamespace(get=lambda cid: Container(0)))
    assert rt.exec("c", "true", timeout=2) == (0, "out")
