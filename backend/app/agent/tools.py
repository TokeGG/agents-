import base64
import shlex
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy.orm import Session

from app.connectors.registry import create_pending, find_active, find_pending
from app.connectors.secret_injection import provision_secret_file
from app.models import Event, Task
from app.sandbox.runtime import truncate_output


@dataclass
class ToolContext:
    task_id: int
    session: Session
    scheduler: object | None = None
    sandbox_manager: object | None = None
    browser_service: object | None = None
    vault: object | None = None


class Tool(Protocol):
    name: str
    description: str
    parameters_schema: dict

    def run(self, arguments: dict, ctx: ToolContext) -> dict: ...


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_schema,
            }
            for tool in self._tools.values()
        ]


class _HttpGetTool:
    name = "http_get"
    description = "Fetch a URL via HTTP GET."
    parameters_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.sandbox_manager is not None:
            url = arguments["url"]
            return ctx.sandbox_manager.run_shell(ctx.task_id, f"curl -sS {shlex.quote(url)}")
        response = httpx.get(arguments["url"])
        body = response.text[:500]
        return {
            "ok": True,
            "output": f"status={response.status_code} body={body}",
            "data": None,
        }


http_get_tool = _HttpGetTool()


class _RunShellTool:
    name = "run_shell"
    description = "Run a shell command in the task's sandbox container."
    parameters_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.sandbox_manager is None:
            return _NO_SANDBOX_RESULT
        return ctx.sandbox_manager.run_shell(ctx.task_id, arguments["command"])


run_shell_tool = _RunShellTool()


class _ReadFileTool:
    name = "read_file"
    description = "Read a file from the task's sandbox container."
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.sandbox_manager is None:
            return _NO_SANDBOX_RESULT
        return ctx.sandbox_manager.read_file(ctx.task_id, arguments["path"])


read_file_tool = _ReadFileTool()


class _WriteFileTool:
    name = "write_file"
    description = "Write a file in the task's sandbox container."
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.sandbox_manager is None:
            return _NO_SANDBOX_RESULT
        return ctx.sandbox_manager.write_file(ctx.task_id, arguments["path"], arguments["content"])


write_file_tool = _WriteFileTool()


_NO_SANDBOX_RESULT = {
    "ok": False,
    "output": "sandboxed execution is not available for this task",
    "data": None,
}


class _EditFileTool:
    name = "edit_file"
    description = "Replace a snippet of text in a file in the task's sandbox container."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.sandbox_manager is None:
            return _NO_SANDBOX_RESULT
        return ctx.sandbox_manager.edit_file(
            ctx.task_id, arguments["path"], arguments["old"], arguments["new"]
        )


edit_file_tool = _EditFileTool()


class _AskUserTool:
    name = "ask_user"
    description = "Ask the user a clarifying question and pause until they answer."
    parameters_schema = {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        ctx.session.add(
            Event(task_id=ctx.task_id, type="ask_user", payload={"question": arguments["question"]})
        )
        ctx.session.commit()
        return {"ok": True, "output": "awaiting user answer", "data": None}


ask_user_tool = _AskUserTool()


class _ReportGapTool:
    name = "report_gap"
    description = "Report that a task cannot be completed as requested, with concrete unblock options."
    parameters_schema = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reason", "options"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        ctx.session.add(
            Event(
                task_id=ctx.task_id,
                type="report_gap",
                payload={"reason": arguments["reason"], "options": arguments["options"]},
            )
        )
        ctx.session.commit()
        return {"ok": True, "output": "gap reported", "data": None}


report_gap_tool = _ReportGapTool()


# Caps on task delegation: how many levels deep a chain of delegate_task calls
# may nest, and how many concurrently-alive (non-terminal) children a single
# parent task may fan out to. Both guard against a model runaway-delegating
# forever or spawning unbounded subtasks.
MAX_DELEGATION_DEPTH = 5
MAX_CHILDREN_PER_TASK = 10

TERMINAL_TASK_STATUSES = {"completed", "failed", "interrupted"}

DELEGATION_DEPTH_OPTIONS = [
    "complete the remaining work directly instead of delegating further",
    "rephrase the task to need fewer delegation levels",
]

DELEGATION_FANOUT_OPTIONS = [
    "wait for an existing subtask to finish before delegating another",
    "combine several subtasks into one",
]


class _DelegateTaskTool:
    name = "delegate_task"
    description = "Delegate part of the task to a new subtask, run either in the background or blocking until it finishes."
    parameters_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "mode": {"type": "string", "enum": ["blocking", "background"]},
            "agent_type": {"type": "string"},
        },
        "required": ["description", "mode"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        parent = ctx.session.get(Task, ctx.task_id)

        if parent.delegation_depth >= MAX_DELEGATION_DEPTH:
            ctx.session.add(
                Event(
                    task_id=ctx.task_id,
                    type="report_gap",
                    payload={
                        "reason": f"delegation too deeply nested (max depth {MAX_DELEGATION_DEPTH})",
                        "options": DELEGATION_DEPTH_OPTIONS,
                    },
                )
            )
            ctx.session.commit()
            return {"ok": False, "output": "delegation refused: too deeply nested", "data": None}

        active_children = (
            ctx.session.query(Task)
            .filter(
                Task.parent_task_id == ctx.task_id,
                Task.status.notin_(TERMINAL_TASK_STATUSES),
            )
            .count()
        )
        if active_children >= MAX_CHILDREN_PER_TASK:
            ctx.session.add(
                Event(
                    task_id=ctx.task_id,
                    type="report_gap",
                    payload={
                        "reason": f"too many active subtasks already (max {MAX_CHILDREN_PER_TASK})",
                        "options": DELEGATION_FANOUT_OPTIONS,
                    },
                )
            )
            ctx.session.commit()
            return {"ok": False, "output": "delegation refused: too many active subtasks", "data": None}

        child = Task(
            description=arguments["description"],
            status="queued",
            agent_type=arguments.get("agent_type", "general"),
            parent_task_id=ctx.task_id,
            delegation_depth=parent.delegation_depth + 1,
        )
        ctx.session.add(child)
        # For a blocking delegation, the parent's status must already read
        # "awaiting_subtask" in the DB *before* the child is enqueued (and so
        # before it can possibly run and terminate). Otherwise there is a
        # window where a fast child reaches a terminal state while the parent
        # row still says "running" (the loop hasn't yet set
        # "awaiting_subtask", which only happens after this tool call
        # returns) -- Scheduler._on_task_terminal would then see a "running"
        # parent, correctly decide there's nothing to wake, and the parent
        # would commit "awaiting_subtask" moments later with no one left to
        # ever re-enqueue it. Setting the status here, in the same commit as
        # the child row, closes that window: by the time the child can
        # possibly finish, the parent already reads "awaiting_subtask".
        if arguments["mode"] == "blocking":
            parent.status = "awaiting_subtask"
        ctx.session.commit()
        ctx.scheduler.enqueue(ctx.session, child.id)
        return {
            "ok": True,
            "output": f"delegated as subtask {child.id}",
            "data": {"subtask_id": child.id},
        }


delegate_task_tool = _DelegateTaskTool()


class _CheckSubtaskTool:
    name = "check_subtask"
    description = "Check the status of a previously delegated subtask."
    parameters_schema = {
        "type": "object",
        "properties": {"subtask_id": {"type": "integer"}},
        "required": ["subtask_id"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        child = ctx.session.get(Task, arguments["subtask_id"])
        if child is None:
            return {"ok": False, "output": "no such subtask", "data": None}
        return {"ok": True, "output": f"subtask status: {child.status}", "data": {"status": child.status}}


check_subtask_tool = _CheckSubtaskTool()


_NO_BROWSER_RESULT = {
    "ok": False,
    "output": "browser automation is not available for this task",
    "data": None,
}


class _BrowseTool:
    name = "browse"
    description = "Navigate the task's browser context to a URL and return the page title and visible text."
    parameters_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.browser_service is None:
            return _NO_BROWSER_RESULT
        try:
            title, text = ctx.browser_service.navigate(ctx.task_id, arguments["url"])
        except PlaywrightError as exc:
            return {"ok": False, "output": f"browse failed: {exc}", "data": None}
        return {"ok": True, "output": truncate_output(f"{title}\n{text}"), "data": None}


browse_tool = _BrowseTool()


class _DownloadFileTool:
    name = "download_file"
    description = (
        "Download a URL's bytes via the task's browser context into the task's "
        "sandbox volume, so the file is usable by run_shell/read_file afterward."
    )
    parameters_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "save_as": {"type": "string"}},
        "required": ["url", "save_as"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        if ctx.browser_service is None or ctx.sandbox_manager is None:
            return _NO_BROWSER_RESULT
        try:
            body = ctx.browser_service.download(ctx.task_id, arguments["url"])
        except PlaywrightError as exc:
            return {"ok": False, "output": f"download failed: {exc}", "data": None}
        # `SandboxManager.write_file` takes a string (Task 2's signature); downloaded
        # content is arbitrary bytes, so it is base64-encoded here and decoded on the
        # shell side via `base64 -d`, rather than extending write_file to accept
        # bytes. This keeps write_file's existing string-only contract (and every
        # other caller of it) untouched, at the cost of a slightly larger
        # over-the-wire payload for downloads specifically.
        encoded = base64.b64encode(body).decode("ascii")
        marker = f"__B64_{uuid.uuid4().hex}__"
        command = (
            f"base64 -d << '{marker}' > {shlex.quote(arguments['save_as'])}\n"
            f"{encoded}\n{marker}\n"
        )
        return ctx.sandbox_manager.run_shell(ctx.task_id, command)


download_file_tool = _DownloadFileTool()


class _RequestConnectorTool:
    name = "request_connector"
    description = (
        "Request a connector (a small tool integrating an external service). "
        "Reuses an existing active or pending connector for the same service/purpose "
        "if one exists, otherwise delegates a background coding subtask to build one."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "purpose": {"type": "string"},
        },
        "required": ["service_name", "purpose"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        service_name = arguments["service_name"]
        purpose = arguments["purpose"]

        active = find_active(ctx.session, service_name, purpose)
        if active is not None:
            return {
                "ok": True,
                "output": f"connector '{active.name}' already active for {service_name}",
                "data": {"connector_name": active.name},
            }

        pending = find_pending(ctx.session, service_name)
        if pending is not None:
            return {
                "ok": True,
                "output": f"a connector for {service_name} is already awaiting approval",
                "data": None,
            }

        forge_brief = (
            f"Build and sandbox-test an integration for the service '{service_name}'. "
            f"Purpose: {purpose}. Use vault_provision_test_credential('{service_name}') to "
            "get a credential file path if one exists (read it by path, never hardcode a "
            "value). Write a single Python module exposing a function with the shape "
            "(name, description, parameters_schema, and a callable entry point reading JSON "
            "arguments from stdin and printing a JSON {'ok','output','data'} result to "
            "stdout). When it passes its own test, call register_connector(...) with that "
            "module's source as source_code. If no credential exists, test against a stub "
            "you write yourself and call report_gap with options to add a credential to the "
            "vault and retry."
        )
        return delegate_task_tool.run(
            {"description": forge_brief, "mode": "background", "agent_type": "coding"}, ctx
        )


request_connector_tool = _RequestConnectorTool()


class _RegisterConnectorTool:
    name = "register_connector"
    description = "Register a newly built connector for human approval."
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "service_name": {"type": "string"},
            "purpose": {"type": "string"},
            "parameters_schema": {"type": "object"},
            "source_code": {"type": "string"},
            "required_hosts": {"type": "array", "items": {"type": "string"}},
            "test_transcript": {"type": "object"},
        },
        "required": [
            "name",
            "service_name",
            "purpose",
            "parameters_schema",
            "source_code",
            "required_hosts",
            "test_transcript",
        ],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        row = create_pending(
            ctx.session,
            name=arguments["name"],
            service_name=arguments["service_name"],
            purpose=arguments["purpose"],
            parameters_schema=arguments["parameters_schema"],
            source_code=arguments["source_code"],
            required_hosts=arguments["required_hosts"],
            requested_by_task_id=ctx.task_id,
            test_transcript=arguments["test_transcript"],
        )
        return {
            "ok": True,
            "output": f"connector '{row.name}' registered, pending review",
            "data": {"connector_id": row.id},
        }


register_connector_tool = _RegisterConnectorTool()


class _VaultProvisionTestCredentialTool:
    name = "vault_provision_test_credential"
    description = (
        "Provision a vault-stored credential for a service into the task's sandbox "
        "as a file, returning its path (never the raw secret value)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {"service_name": {"type": "string"}},
        "required": ["service_name"],
    }

    def run(self, arguments: dict, ctx: ToolContext) -> dict:
        service_name = arguments["service_name"]
        if ctx.vault is None:
            return {
                "ok": True,
                "output": f"no credential configured for {service_name}",
                "data": {"path": None},
            }
        secret = ctx.vault.get(service_name)
        if secret is None:
            return {
                "ok": True,
                "output": f"no credential configured for {service_name}",
                "data": {"path": None},
            }
        path = provision_secret_file(ctx.sandbox_manager, ctx.task_id, secret)
        return {
            "ok": True,
            "output": f"credential available at {path}",
            "data": {"path": path},
        }


vault_provision_test_credential_tool = _VaultProvisionTestCredentialTool()
