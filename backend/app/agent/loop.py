import json

import httpx
from sqlalchemy.orm import Session

from app.agent.model_adapter import ModelAdapter
from app.agent.permissions import needs_approval
from app.agent.tools import ToolContext, ToolRegistry
from app.models import Event, Task

UNKNOWN_TOOL_OPTIONS = [
    "register this tool via the connector forge (not yet built)",
    "rephrase the task to avoid it",
]

MAX_STEP_LIMIT_OPTIONS = [
    "increase the step limit",
    "rephrase the task to be more specific",
    "break it into a smaller task",
]

# Caps how many model turns a single task may take, so a model that loops on
# tool calls or report_gap without converging cannot run (and spend tokens)
# forever.
MAX_STEPS = 25


def _add_event(session: Session, task_id: int, type_: str, payload: dict) -> Event:
    event = Event(task_id=task_id, type=type_, payload=payload)
    session.add(event)
    session.commit()
    return event


def _tool_call_message(call_id: str, tool_name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _build_initial_messages(session: Session, task: Task) -> list[dict]:
    """Replay persisted Events into a message history, so a resumed task
    (answer/approve) continues the same conversation instead of restarting it."""
    messages = [{"role": "user", "content": task.description}]
    events = session.query(Event).filter_by(task_id=task.id).order_by(Event.id).all()
    last_call_id: str | None = None
    for event in events:
        if event.type == "model_step":
            step = event.payload
            if step.get("type") == "tool_call":
                last_call_id = f"call_{event.id}"
                messages.append(_tool_call_message(last_call_id, step["tool_name"], step["arguments"]))
            elif step.get("type") == "malformed_tool_call":
                messages.append({"role": "assistant", "content": "(malformed tool call, ignored)"})
            else:
                messages.append({"role": "assistant", "content": step.get("content", "")})
        elif event.type == "tool_result":
            messages.append(
                {"role": "tool", "tool_call_id": last_call_id, "content": event.payload.get("output", "")}
            )
        elif event.type == "user_answer":
            messages.append({"role": "user", "content": event.payload.get("answer", "")})
        elif event.type == "approval_response":
            messages.append({"role": "user", "content": f"approved={event.payload.get('approved')}"})
    return messages


def run_task_loop(task_id: int, session: Session, adapter: ModelAdapter, registry: ToolRegistry) -> None:
    task = session.get(Task, task_id)
    messages = _build_initial_messages(session, task)
    steps_taken = 0

    while True:
        if steps_taken >= MAX_STEPS:
            _add_event(
                session,
                task_id,
                "report_gap",
                {
                    "reason": f"maximum step limit ({MAX_STEPS}) reached without completing the task",
                    "options": MAX_STEP_LIMIT_OPTIONS,
                },
            )
            task.status = "failed"
            session.commit()
            return
        steps_taken += 1

        step = adapter.complete(messages, registry.specs())
        step_event = _add_event(session, task_id, "model_step", dict(step))

        if step["type"] == "final_answer":
            messages.append({"role": "assistant", "content": step.get("content", "")})
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

        if step["type"] == "malformed_tool_call":
            messages.append({"role": "assistant", "content": "(malformed tool call, ignored)"})
            _add_event(
                session,
                task_id,
                "tool_result",
                {"ok": False, "output": f"malformed tool call arguments: {step.get('raw', '')}", "data": None},
            )
            continue

        tool_name = step["tool_name"]
        arguments = step["arguments"]
        call_id = f"call_{step_event.id}"
        messages.append(_tool_call_message(call_id, tool_name, arguments))
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
        except (KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
            result = {"ok": False, "output": f"tool call failed: {exc}", "data": None}

        _add_event(session, task_id, "tool_result", result)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result["output"]})

        if tool_name == "ask_user" and result.get("ok"):
            task.status = "awaiting_answer"
            session.commit()
            return
