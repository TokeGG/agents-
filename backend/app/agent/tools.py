from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from app.models import Event


@dataclass
class ToolContext:
    task_id: int
    session: Session


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
