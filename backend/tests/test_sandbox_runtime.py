import sys
import types
from unittest.mock import MagicMock

from app.sandbox.runtime import DockerContainerRuntime, FakeContainerRuntime


def test_fake_runtime_exec_runs_in_volume_dir(tmp_path, monkeypatch):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    runtime.create_volume("vol-1")
    container_id = runtime.start_container("c-1", "vol-1", "agents-sandbox:latest", "2g", 1.0, "sandbox-net")
    exit_code, output = runtime.exec(container_id, "echo hello > out.txt && cat out.txt", timeout=10)
    assert exit_code == 0
    assert "hello" in output


def test_fake_runtime_truncates_long_output(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    runtime.create_volume("vol-2")
    container_id = runtime.start_container("c-2", "vol-2", "img", "2g", 1.0, "net")
    _, output = runtime.exec(container_id, "python3 -c \"print('x' * 10000)\"", timeout=10)
    assert len(output) <= 4000


def test_docker_runtime_start_container_passes_resource_limits(monkeypatch):
    client = MagicMock()
    client.containers.run.return_value.id = "abc123"
    fake_docker = types.ModuleType("docker")
    fake_docker.from_env = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    runtime = DockerContainerRuntime()
    result = runtime.start_container("c-3", "vol-3", "agents-sandbox:latest", "2g", 1.5, "sandbox-net")

    assert result == "abc123"
    kwargs = client.containers.run.call_args.kwargs
    assert client.containers.run.call_args.args[0] == "agents-sandbox:latest"
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["network"] == "sandbox-net"
    assert kwargs["volumes"] == {"vol-3": {"bind": "/workspace", "mode": "rw"}}
    assert kwargs["working_dir"] == "/workspace"


def test_validate_sandbox_egress_config_raises_when_unset(monkeypatch):
    import pytest as _pytest

    from app.sandbox.runtime import validate_sandbox_egress_config

    monkeypatch.delenv("SANDBOX_EGRESS_ALLOWLIST", raising=False)
    with _pytest.raises(RuntimeError):
        validate_sandbox_egress_config()


def test_validate_sandbox_egress_config_raises_when_empty(monkeypatch):
    import pytest as _pytest

    from app.sandbox.runtime import validate_sandbox_egress_config

    monkeypatch.setenv("SANDBOX_EGRESS_ALLOWLIST", "")
    with _pytest.raises(RuntimeError):
        validate_sandbox_egress_config()


def test_validate_sandbox_egress_config_passes_when_set(monkeypatch):
    from app.sandbox.runtime import validate_sandbox_egress_config

    monkeypatch.setenv("SANDBOX_EGRESS_ALLOWLIST", "api.example.com,pypi.org")
    validate_sandbox_egress_config()
