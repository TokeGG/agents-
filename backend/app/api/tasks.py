import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.agent.model_adapter import ModelAdapter, OpenAICompatAdapter
from app.agent.tools import ToolRegistry, ask_user_tool, http_get_tool, report_gap_tool
from app.db import get_session
from app.models import Event, Rating, Task
from app.agent.tools import ToolContext
from app.schemas import (
    AnswerTaskRequest,
    ApproveTaskRequest,
    CreateTaskRequest,
    CreateTaskResponse,
    RateTaskRequest,
)

router = APIRouter(dependencies=[Depends(require_auth)])


def get_adapter() -> ModelAdapter:
    return OpenAICompatAdapter(
        base_url=os.environ.get("MODEL_API_BASE", ""),
        api_key=os.environ.get("MODEL_API_KEY", ""),
        model_name=os.environ.get("MODEL_NAME", "default"),
    )


def get_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(http_get_tool)
    registry.register(ask_user_tool)
    registry.register(report_gap_tool)
    return registry


def get_scheduler(request: Request):
    return request.app.state.scheduler


@router.post("/tasks", status_code=201, response_model=CreateTaskResponse)
def create_task(
    body: CreateTaskRequest,
    session: Session = Depends(get_session),
    scheduler=Depends(get_scheduler),
):
    task = Task(description=body.description, status="queued", full_auto=body.full_auto)
    session.add(task)
    session.commit()
    scheduler.enqueue(session, task.id)
    return CreateTaskResponse(id=task.id, status=task.status)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, session: Session = Depends(get_session)):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    events = session.query(Event).filter_by(task_id=task_id).order_by(Event.id).all()
    return {
        "task": {
            "id": task.id,
            "description": task.description,
            "status": task.status,
            "full_auto": task.full_auto,
        },
        "events": [{"type": e.type, "payload": e.payload} for e in events],
    }


@router.post("/tasks/{task_id}/rate", status_code=201)
def rate_task(task_id: int, body: RateTaskRequest, session: Session = Depends(get_session)):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="task is not completed yet")
    rating = Rating(task_id=task_id, value=body.value, comment=body.comment)
    session.add(rating)
    session.commit()
    return {"id": rating.id}


@router.post("/tasks/{task_id}/answer", status_code=201)
def answer_task(
    task_id: int,
    body: AnswerTaskRequest,
    session: Session = Depends(get_session),
    scheduler=Depends(get_scheduler),
):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "awaiting_answer":
        raise HTTPException(status_code=409, detail="task is not awaiting an answer")
    session.add(Event(task_id=task_id, type="user_answer", payload={"answer": body.answer}))
    session.commit()
    scheduler.enqueue(session, task_id, front=True)
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/approve", status_code=201)
def approve_task(
    task_id: int,
    body: ApproveTaskRequest,
    session: Session = Depends(get_session),
    registry: ToolRegistry = Depends(get_registry),
    scheduler=Depends(get_scheduler),
):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="task is not awaiting approval")

    pending = (
        session.query(Event)
        .filter_by(task_id=task_id, type="approval_request")
        .order_by(Event.id.desc())
        .first()
    )
    if pending is None:
        raise HTTPException(status_code=409, detail="no pending approval request found for this task")
    tool_name = pending.payload["tool_name"]
    arguments = pending.payload["arguments"]

    if body.approved:
        tool = registry.get(tool_name)
        if tool is None:
            result = {
                "ok": False,
                "output": f"tool '{tool_name}' is no longer registered",
                "data": None,
            }
        else:
            ctx = ToolContext(task_id=task_id, session=session)
            try:
                result = tool.run(arguments, ctx)
            except (KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
                result = {"ok": False, "output": f"tool call failed: {exc}", "data": None}
        session.add(Event(task_id=task_id, type="tool_result", payload=result))
    else:
        session.add(
            Event(
                task_id=task_id,
                type="tool_result",
                payload={"ok": False, "output": "user denied approval for this action", "data": None},
            )
        )

    session.add(Event(task_id=task_id, type="approval_response", payload={"approved": body.approved}))
    session.commit()
    scheduler.enqueue(session, task_id, front=True)
    return {"id": task.id, "status": task.status}
