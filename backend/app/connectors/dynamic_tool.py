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
                secret_path = provision_secret_file(sandbox_manager, task_id, secret)

        invocation = f"python3 {_quote(connector_path)}"
        if secret_path is not None:
            invocation += f" {_quote(secret_path)}"

        marker = f"__ARGS_{uuid.uuid4().hex}__"
        payload = json.dumps(arguments)
        command = f"{invocation} << '{marker}'\n{payload}\n{marker}\n"
        if secret_path is not None:
            # Capture the real exit code of the invocation before removing
            # the secret file, so the secret is shredded unconditionally
            # (whether the script succeeds, fails, or crashes) while the
            # result returned below still reflects the script's own
            # success/failure, not `rm`'s.
            command += f"\nrc=$?\nrm -f {_quote(secret_path)}\nexit $rc\n"

        exec_result = sandbox_manager.run_shell(task_id, command)

        if not exec_result["ok"]:
            output = redact_secret(exec_result["output"], secret)
            return {"ok": False, "output": output, "data": None}

        raw_output = exec_result["output"]
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            output = redact_secret(raw_output, secret)
            return {"ok": False, "output": output, "data": None}

        if not isinstance(parsed, dict):
            output = redact_secret(raw_output, secret)
            return {"ok": False, "output": output, "data": None}

        result = dict(parsed)
        result["output"] = redact_secret(result.get("output", ""), secret)
        return result
