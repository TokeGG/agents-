from sqlalchemy.orm import Session

from app.agent.model_adapter import ModelAdapter
from app.agent.permissions import needs_approval
from app.agent.tools import ToolContext, ToolRegistry
from app.models import Event, Task

UNKNOWN_TOOL_OPTIONS = [
    "register this tool via the connector forge (not yet built)",
    "rephrase the task to avoid it",
]


def _add_event(session: Session, task_id: int, type_: str, payload: dict) -> None:
    session.add(Event(task_id=task_id, type=type_, payload=payload))
    session.commit()


def run_task_loop(task_id: int, session: Session, adapter: ModelAdapter, registry: ToolRegistry) -> None:
    task = session.get(Task, task_id)
    messages = [{"role": "user", "content": task.description}]

    while True:
        step = adapter.complete(messages, registry.specs())
        _add_event(session, task_id, "model_step", dict(step))
        messages.append({"role": "assistant", "content": str(step)})

        if step["type"] == "final_answer":
            task.status = "completed"
            session.commit()
            _add_event(
                session,
                task_id,
                "reflection",
                {
                    "summary": step["content"],
                    "note": "minimal reflection, see spec §8 for full learning loop",
                },
            )
            return

        tool_name = step["tool_name"]
        arguments = step["arguments"]
        _add_event(session, task_id, "tool_call", {"tool_name": tool_name, "arguments": arguments})

        tool = registry.get(tool_name)
        if tool is None:
            _add_event(
                session,
                task_id,
                "report_gap",
                {"reason": f"no tool named '{tool_name}' is registered", "options": UNKNOWN_TOOL_OPTIONS},
            )
            task.status = "failed"
            session.commit()
            return

        if tool_name not in ("ask_user", "report_gap") and needs_approval(tool_name, task):
            task.status = "awaiting_approval"
            _add_event(session, task_id, "approval_request", {"tool_name": tool_name, "arguments": arguments})
            return

        ctx = ToolContext(task_id=task_id, session=session)
        try:
            result = tool.run(arguments, ctx)
        except (KeyError, TypeError, ValueError) as exc:
            result = {"ok": False, "output": f"tool call failed: {exc}", "data": None}

        _add_event(session, task_id, "tool_result", result)
        messages.append({"role": "tool", "content": result["output"]})

        if tool_name == "ask_user" and result.get("ok"):
            task.status = "awaiting_answer"
            session.commit()
            return
