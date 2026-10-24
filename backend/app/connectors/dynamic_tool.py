"""DynamicConnectorTool: sandboxed execution of agent-forged connector code.

Wraps a `Connector` row as a `Tool` (see `app.agent.tools.Tool`): its
`.name`/`.description`/`.parameters_schema` mirror the connector row, and
`.run` executes the connector's `source_code` inside the task's sandbox
container, feeding it `arguments` as JSON on stdin and (if the connector's
`service_name` has a vault credential) a provisioned secret file path as
`sys.argv[1]`.

Security-critical invariants:
- The secret file is removed from the sandbox unconditionally after the
  call, whether the connector script succeeds, fails, or crashes.
- The raw secret value is redacted out of whatever the connector printed
  before the result is returned, so it never reaches persisted state or
  the model.
"""

from __future__ import annotations

import json
import shlex
import uuid

from app.agent.tools import _NO_SANDBOX_RESULT
from app.connectors.registry import Connector
from app.connectors.secret_injection import provision_secret_file, redact_secret


def _quote(value: str) -> str:
    """Shell-quote a string for safe inclusion in a `sh -c`-style command."""
    return "'" + value.replace("'", "'\\''") + "'"


class DynamicConnectorTool:
    def __init__(self, connector: Connector):
        self.connector = connector
        self.name = connector.name
        self.description = connector.purpose
        self.parameters_schema = connector.parameters_schema
        # Tracks task ids whose sandbox volume already has this connector's
        # source_code cached at `connectors/<name>.py`, so repeated calls
        # (via this same tool instance, in this process) don't re-write the
        # unchanging source file every time. Instance-level (one set per
        # connector) rather than module-level/global: a given
        # DynamicConnectorTool is already scoped to one connector, so no
        # extra key component for connector.name is needed, and keeping the
        # cache on the instance (rather than shared across every connector
        # tool in the process) means two independently-constructed tools
        # never see each other's cache state, which matters because task
        # ids are only unique within a given SandboxManager/DB — reusing a
        # task id across a different manager (e.g. two separate test runs,
        # or any scenario with more than one sandbox backing store) must
        # not short-circuit a write against the new, unrelated volume.
        self._cached_for_task: set[int] = set()

    def run(self, arguments: dict, ctx) -> dict:
        if ctx.sandbox_manager is None:
            return _NO_SANDBOX_RESULT

        sandbox_manager = ctx.sandbox_manager
        task_id = ctx.task_id
        connector_path = f"connectors/{self.connector.name}.py"

        if task_id not in self._cached_for_task:
            mkdir = sandbox_manager.run_shell(task_id, "mkdir -p connectors")
            if not mkdir["ok"]:
                return {"ok": False, "output": mkdir["output"], "data": None}
            write_result = sandbox_manager.write_file(
                task_id, connector_path, self.connector.source_code
            )
            if not write_result["ok"]:
                return {"ok": False, "output": write_result["output"], "data": None}
            self._cached_for_task.add(task_id)

        secret = None
        secret_path = None
        vault = getattr(ctx, "vault", None)
        if vault is not None:
            secret = vault.get(self.connector.service_name)
            if secret is not None:
                try:
                    secret_path = provision_secret_file(sandbox_manager, task_id, secret)
                except RuntimeError as exc:
                    return {"ok": False, "output": f"failed to provision credential: {exc}", "data": None}

        invocation = f"python3 {_quote(connector_path)}"
        if secret_path is not None:
            invocation += f" {_quote(secret_path)}"

        marker = f"__ARGS_{uuid.uuid4().hex}__"
        payload = json.dumps(arguments)
        command = f"{invocation} << '{marker}'\n{payload}\n{marker}\n"
        if secret_path is not None:
            # First line of defense: capture the real exit code of the
            # invocation before removing the secret file, so the secret is
            # shredded unconditionally within the same shell session
            # (whether the script succeeds, fails, or crashes) while the
            # result returned below still reflects the script's own
            # success/failure, not `rm`'s. This alone is not sufficient: a
            # command timeout kills the whole `sh -c` session (including
            # this `rm -f`) before it runs, and `run_shell` itself can raise
            # (e.g. a stopped container) after the secret file was already
            # written. The `finally` block below is the second, independent
            # line of defense that covers exactly those cases.
            command += f"\nrc=$?\nrm -f {_quote(secret_path)}\nexit $rc\n"

        try:
            exec_result = sandbox_manager.run_shell(task_id, command)
        finally:
            if secret_path is not None:
                self._best_effort_shred(sandbox_manager, task_id, secret_path)

        if not exec_result["ok"]:
            output = redact_secret(exec_result["output"], secret)
            return {"ok": False, "output": output, "data": None}

        raw_output = exec_result["output"]
        # Redact the raw JSON text before parsing, not just result["output"]
        # after parsing: a connector could echo the secret into ANY field
        # (e.g. "data"), and the model sees the whole parsed result, not
        # just "output".
        redacted_output = redact_secret(raw_output, secret)
        try:
            parsed = json.loads(redacted_output)
        except json.JSONDecodeError:
            return {"ok": False, "output": redacted_output, "data": None}

        if not isinstance(parsed, dict):
            return {"ok": False, "output": redacted_output, "data": None}

        return parsed

    @staticmethod
    def _best_effort_shred(sandbox_manager, task_id: int, secret_path: str) -> None:
        """Defense-in-depth cleanup for the secret file, run as a SEPARATE
        `run_shell` call outside the (possibly timed-out or already-failed)
        invocation command. Covers the cases the in-shell `rm -f` cannot:
        a command timeout kills the whole shell session before its `rm -f`
        runs, and `run_shell` itself can raise after the secret file was
        already written (e.g. a stopped container, a Docker API error).
        Any error here is swallowed: this is cleanup, not the primary
        success/failure path, and must never mask or replace the real
        result/exception from the invocation itself.
        """
        try:
            sandbox_manager.run_shell(task_id, f"rm -f {shlex.quote(secret_path)}")
        except Exception:
            pass
