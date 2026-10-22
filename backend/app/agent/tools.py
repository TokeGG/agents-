import base64
import shlex
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from app.models import Event, Task
from app.sandbox.runtime import truncate_output


@dataclass
class ToolContext:
    task_id: int
    session: Session
    scheduler: object | None = None
    sandbox_manager: object | None = None
    browser_service: object | None = None


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
        context = ctx.browser_service.get_or_create_context(ctx.task_id)
        page = context.new_page()
        page.goto(arguments["url"])
        title = page.title()
        text = page.inner_text("body")
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
        context = ctx.browser_service.get_or_create_context(ctx.task_id)
        response = context.request.get(arguments["url"])
        body = response.body()
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
