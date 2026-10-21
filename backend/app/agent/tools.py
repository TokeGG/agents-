from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from app.models import Event, Task


@dataclass
class ToolContext:
    task_id: int
    session: Session
    scheduler: object | None = None


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
        response = httpx.get(arguments["url"])
        body = response.text[:500]
        return {
            "ok": True,
            "output": f"status={response.status_code} body={body}",
            "data": None,
        }


http_get_tool = _HttpGetTool()


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
MAX_DELEGATION_DEPTH = 3
MAX_CHILDREN_PER_TASK = 5

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
