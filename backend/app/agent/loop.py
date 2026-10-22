import json
import logging
import threading

import httpx
from sqlalchemy.orm import Session

from app.agent.model_adapter import ModelAdapter
from app.agent.model_tiering import ToolFailureTracker
from app.agent.permissions import needs_approval
from app.agent.tools import ToolContext, ToolRegistry
from app.models import Event, Task
from app.sandbox.runtime import SandboxSlotUnavailable

logger = logging.getLogger(__name__)

UNKNOWN_TOOL_OPTIONS = [
    "register this tool via the connector forge (not yet built)",
    "rephrase the task to avoid it",
]

INTERRUPT_OPTIONS = [
    "retry the task",
    "it stopped partway — review its trace before retrying",
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
        elif event.type == "subtask_update":
            # Informational context for the model, not a literal response to
            # a specific tool call (the tool_call_id that originally
            # requested this delegation was already answered by the
            # replayed tool_result "delegated as subtask N") -- a real
            # OpenAI-style API requires each tool_call_id to answer exactly
            # one pending call, so this must not be a "tool" role message.
            payload = event.payload
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"subtask {payload.get('subtask_id')} finished with status "
                        f"'{payload.get('status')}': {payload.get('content')}"
                    ),
                }
            )
    return messages


def _interrupt_if_requested(session: Session, task: Task, interrupt_event) -> bool:
    """If a kill has been signalled, marks the task "interrupted" (with the
    kill-switch report_gap) and returns True. Called at the top of every step
    and again right before every early-return pause point (ask_user,
    awaiting_approval, blocking delegate_task): a kill landing while a step
    is in flight would otherwise be silently dropped once the task parks in
    a waiting status, since the top-of-loop check never runs again."""
    if interrupt_event is None or not interrupt_event.is_set():
        return False
    _add_event(
        session,
        task.id,
        "report_gap",
        {
            "reason": "stopped by kill switch",
            "options": INTERRUPT_OPTIONS,
        },
    )
    task.status = "interrupted"
    session.commit()
    return True


def _review_flags_problem(review_step: dict) -> bool:
    """True if the reviewer's response amounts to flagging a problem: either a
    tool_call to report_gap, or a plain message that reads as raising a concern
    rather than a clean bill of health."""
    if review_step["type"] == "tool_call" and review_step.get("tool_name") == "report_gap":
        return True
    if review_step["type"] == "final_answer":
        content = (review_step.get("content") or "").lower()
        return "problem" in content or "issue" in content or "bug" in content or "concern" in content
    return False


def _review_feedback_text(review_step: dict) -> str:
    """Renders the reviewer's flagged finding as text the worker's fix call can see."""
    if review_step["type"] == "tool_call":
        return (
            f"report_gap(reason={review_step.get('arguments', {}).get('reason')!r}, "
            f"arguments={review_step.get('arguments')!r})"
        )
    return review_step.get("content") or ""


def _run_post_task_review(
    session: Session,
    task: Task,
    messages: list[dict],
    worker_adapter: ModelAdapter,
    reviewer_adapter: ModelAdapter,
    registry: ToolRegistry,
    sandbox_manager,
    scheduler=None,
) -> None:
    """Bounded, additive-only post-task review: one reviewer call to look at the
    sandbox's git diff, and if it flags a problem, exactly one worker fix call
    that sees the reviewer's finding and, if it returns a tool call, actually
    executes it. Never loops back into review again, regardless of what the
    fix step produces. Any exception here is caught by the caller so a review-
    pass failure never stops the task from completing."""
    diff_result = sandbox_manager.run_shell(task.id, "git diff")
    review_messages = messages + [
        {
            "role": "user",
            "content": (
                "Review the following git diff against the task description and flag any "
                "problem you see.\n\nTask description:\n"
                f"{task.description}\n\nGit diff:\n{diff_result.get('output', '')}"
            ),
        }
    ]
    review_step = reviewer_adapter.complete(review_messages, registry.specs())
    flagged = _review_flags_problem(review_step)
    fix_summary = "not attempted (no problem flagged)"
    if flagged:
        feedback_text = _review_feedback_text(review_step)
        fix_messages = messages + [
            {
                "role": "user",
                "content": (
                    "A reviewer examined your work and flagged the following problem. "
                    "Address it if you can.\n\n" + feedback_text
                ),
            }
        ]
        fix_step = worker_adapter.complete(fix_messages, registry.specs())
        if fix_step["type"] == "tool_call":
            tool = registry.get(fix_step["tool_name"])
            if tool is None:
                fix_summary = f"worker called unknown tool '{fix_step['tool_name']}'"
            else:
                ctx = ToolContext(
                    task_id=task.id, session=session, scheduler=scheduler, sandbox_manager=sandbox_manager
                )
                try:
                    result = tool.run(fix_step["arguments"], ctx)
                    fix_summary = f"executed tool '{fix_step['tool_name']}': {result}"
                except Exception as exc:  # noqa: BLE001 - this is a best-effort fix step
                    fix_summary = f"tool '{fix_step['tool_name']}' raised: {exc}"
        else:
            fix_summary = f"worker response: {fix_step.get('content', '')}"
    _add_event(
        session,
        task.id,
        "post_task_review",
        {"flagged": flagged, "fix_summary": fix_summary},
    )


def run_task_loop(
    task_id: int,
    session: Session,
    worker_adapter: ModelAdapter,
    registry: ToolRegistry,
    scheduler=None,
    interrupt_event: threading.Event | None = None,
    sandbox_manager=None,
    reviewer_adapter: ModelAdapter | None = None,
) -> None:
    if reviewer_adapter is None:
        reviewer_adapter = worker_adapter
    task = session.get(Task, task_id)
    messages = _build_initial_messages(session, task)
    # Persisted on the Task row so the count survives pause/resume across
    # separate run_task_loop calls.
    steps_taken = task.steps_taken
    # Scoped to this single run_task_loop call: NOT persisted across
    # pause/resume. A resumed task (answer/approve) gets a fresh tracker and
    # starts clean rather than replaying history to re-derive a failure
    # count -- see model_tiering.py's module docstring for the rationale.
    tool_failure_tracker = ToolFailureTracker()

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
        if _interrupt_if_requested(session, task, interrupt_event):
            return
        steps_taken += 1
        task.steps_taken = steps_taken
        session.commit()

        adapter = reviewer_adapter if tool_failure_tracker.should_escalate() else worker_adapter
        step = adapter.complete(messages, registry.specs())
        step_event = _add_event(session, task_id, "model_step", dict(step))

        if step["type"] == "final_answer":
            messages.append({"role": "assistant", "content": step.get("content", "")})
            if (
                reviewer_adapter is not worker_adapter
                and sandbox_manager is not None
                and task_id in getattr(sandbox_manager, "_slots", {})
            ):
                try:
                    _run_post_task_review(
                        session, task, messages, worker_adapter, reviewer_adapter, registry,
                        sandbox_manager, scheduler=scheduler,
                    )
                except Exception:
                    logger.exception("post-task review pass failed for task %s; completing anyway", task_id)
            task.status = "completed"
            # Status and the reflection event are committed together (status
            # is set but not yet committed here) so a concurrent reader never
            # observes "completed" with the reflection event still missing.
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
            if _interrupt_if_requested(session, task, interrupt_event):
                return
            task.status = "awaiting_approval"
            _add_event(session, task_id, "approval_request", {"tool_name": tool_name, "arguments": arguments})
            return

        ctx = ToolContext(task_id=task_id, session=session, scheduler=scheduler, sandbox_manager=sandbox_manager)
        try:
            result = tool.run(arguments, ctx)
        except SandboxSlotUnavailable:
            if _interrupt_if_requested(session, task, interrupt_event):
                return
            task.status = "awaiting_sandbox_slot"
            session.commit()
            return
        except (KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
            result = {"ok": False, "output": f"tool call failed: {exc}", "data": None}
            tool_failure_tracker.record(tool_name, str(exc))

        _add_event(session, task_id, "tool_result", result)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result["output"]})

        if tool_name == "ask_user" and result.get("ok"):
            if _interrupt_if_requested(session, task, interrupt_event):
                return
            task.status = "awaiting_answer"
            session.commit()
            return

        if tool_name == "delegate_task" and result.get("ok") and arguments.get("mode") == "blocking":
            if _interrupt_if_requested(session, task, interrupt_event):
                return
            # delegate_task itself already committed parent.status =
            # "awaiting_subtask" (before enqueueing the child). Re-assigning
            # and re-committing it here could clobber a "queued" status that
            # _on_task_terminal set if the child already finished and woke
            # this parent in between, so just return.
            return
