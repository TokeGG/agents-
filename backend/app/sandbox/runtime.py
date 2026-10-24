"""Container runtime abstraction for sandboxed execution.

Two implementations share one protocol:
- FakeContainerRuntime: local temp directories plus real subprocesses (tests).
- DockerContainerRuntime: thin wrapper around the docker Python SDK (deployment).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Protocol

MAX_OUTPUT_CHARS = 4000

SANDBOX_EGRESS_ALLOWLIST_ENV_VAR = "SANDBOX_EGRESS_ALLOWLIST"


class SandboxSlotUnavailable(Exception):
    """Raised when no sandbox container slot can be allocated."""


def validate_sandbox_egress_config() -> None:
    """Fail closed at startup if the operator hasn't configured an explicit
    network egress allowlist for sandbox containers.

    Sandboxed execution is always-on in this deployment (there is no feature
    flag gating it), so this must be checked unconditionally whenever the app
    starts, before anything else starts up. Without an explicit allowlist, a
    misconfigured deployment would otherwise allow unrestricted network
    egress from inside sandboxes.
    """
    value = os.environ.get(SANDBOX_EGRESS_ALLOWLIST_ENV_VAR, "")
    if not value.strip():
        raise RuntimeError(
            f"{SANDBOX_EGRESS_ALLOWLIST_ENV_VAR} is unset or empty. Refusing to start: "
            "sandbox container network egress must be restricted to an explicit allowlist "
            f"of hosts. Set {SANDBOX_EGRESS_ALLOWLIST_ENV_VAR} to a comma-separated list of "
            "allowed hosts before starting this app."
        )


def truncate_output(text: str) -> str:
    return text[:MAX_OUTPUT_CHARS]


class ContainerRuntime(Protocol):
    def create_volume(self, name: str) -> None: ...

    def start_container(
        self,
        container_id: str,
        volume_name: str,
        image: str,
        memory_limit: str,
        cpu_limit: float,
        network: str,
    ) -> str: ...

    def exec(self, container_id: str, command: str, timeout: int) -> tuple[int, str]: ...

    def stop_container(self, container_id: str) -> None: ...

    def remove_container(self, container_id: str) -> None: ...

    def remove_volume(self, name: str) -> None: ...


class FakeContainerRuntime:
    """In-memory runtime: each volume is a real temp directory; exec runs real shell commands there."""

    def __init__(self, base_dir: Path):
        self._base_dir = Path(base_dir)
        self._containers: dict[str, dict] = {}
        self._stopped: set[str] = set()

    def create_volume(self, name: str) -> None:
        (self._base_dir / name).mkdir(parents=True, exist_ok=True)

    def start_container(
        self,
        container_id: str,
        volume_name: str,
        image: str,
        memory_limit: str,
        cpu_limit: float,
        network: str,
    ) -> str:
        self._containers[container_id] = {
            "volume_dir": self._base_dir / volume_name,
            "image": image,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
            "network": network,
        }
        return container_id

    def exec(self, container_id: str, command: str, timeout: int) -> tuple[int, str]:
        volume_dir = self._containers[container_id]["volume_dir"]
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=volume_dir,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return -1, f"command timed out after {timeout}s"
        return proc.returncode, truncate_output(proc.stdout + proc.stderr)

    def stop_container(self, container_id: str) -> None:
        self._stopped.add(container_id)

    def remove_container(self, container_id: str) -> None:
        self._containers.pop(container_id, None)
        self._stopped.discard(container_id)

    def remove_volume(self, name: str) -> None:
        shutil.rmtree(self._base_dir / name, ignore_errors=True)

    def is_stopped(self, container_id: str) -> bool:
        return container_id in self._stopped


class DockerContainerRuntime:
    """Docker SDK-backed runtime. The `docker` package is imported lazily so this module
    can be imported without it installed."""

    def __init__(self):
        import docker  # lazy: only required when this runtime is actually used

        self._client = docker.from_env()

    def create_volume(self, name: str) -> None:
        self._client.volumes.create(name=name)

    def start_container(
        self,
        container_id: str,
        volume_name: str,
        image: str,
        memory_limit: str,
        cpu_limit: float,
        network: str,
    ) -> str:
        container = self._client.containers.run(
            image,
            name=container_id,
            detach=True,
            mem_limit=memory_limit,
            nano_cpus=int(cpu_limit * 1e9),
            network=network,
            volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
            working_dir="/workspace",
        )
        return container.id

    def exec(self, container_id: str, command: str, timeout: int) -> tuple[int, str]:
        # NOTE: docker's exec_run has no native timeout; `timeout` is accepted for protocol parity.
        container = self._client.containers.get(container_id)
        result = container.exec_run(command, demux=False)
        output = result.output.decode("utf-8", errors="replace") if isinstance(result.output, bytes) else str(result.output)
        return result.exit_code, truncate_output(output)

    def stop_container(self, container_id: str) -> None:
        self._client.containers.get(container_id).stop()

    def remove_container(self, container_id: str) -> None:
        self._client.containers.get(container_id).remove(force=True)

    def remove_volume(self, name: str) -> None:
        self._client.volumes.get(name).remove(force=True)
