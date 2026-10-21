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


def _build_initial_messages(session: Session, task: Task) -> list[dict]:
    """Replay persisted Events into a message history, so a resumed task
    (answer/approve) continues the same conversation instead of restarting it."""
    messages = [{"role": "user", "content": task.description}]
    events = session.query(Event).filter_by(task_id=task.id).order_by(Event.id).all()
    for event in events:
        if event.type == "model_step":
            messages.append({"role": "assistant", "content": str(event.payload)})
        elif event.type == "tool_result":
            messages.append({"role": "tool", "content": event.payload.get("output", "")})
        elif event.type == "user_answer":
            messages.append({"role": "user", "content": event.payload.get("answer", "")})
        elif event.type == "approval_response":
            messages.append({"role": "user", "content": f"approved={event.payload.get('approved')}"})
    return messages


def run_task_loop(task_id: int, session: Session, adapter: ModelAdapter, registry: ToolRegistry) -> None:
    task = session.get(Task, task_id)
    messages = _build_initial_messages(session, task)

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
