from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.tasks import get_scheduler
from app.auth import require_auth
from app.db import get_session
from app.models import Task

router = APIRouter(dependencies=[Depends(require_auth)])

NEEDS_YOU_STATUSES = ("awaiting_answer", "awaiting_approval")
DONE_STATUSES = ("completed", "failed", "interrupted")
DONE_LIMIT = 50


def _serialize(task: Task, session: Session) -> dict:
    children = session.query(Task).filter_by(parent_task_id=task.id).order_by(Task.id).all()
    return {
        "id": task.id,
        "description": task.description,
        "status": task.status,
        "agent_type": task.agent_type,
        "parent_task_id": task.parent_task_id,
        "children": [_serialize(child, session) for child in children],
    }


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), scheduler=Depends(get_scheduler)):
    queued = session.query(Task).filter_by(status="queued").order_by(Task.id).all()
    running = session.query(Task).filter_by(status="running").order_by(Task.id).all()
    needs_you = (
        session.query(Task).filter(Task.status.in_(NEEDS_YOU_STATUSES)).order_by(Task.id).all()
    )
    done = (
        session.query(Task)
        .filter(Task.status.in_(DONE_STATUSES), Task.parent_task_id.is_(None))
        .order_by(Task.id.desc())
        .limit(DONE_LIMIT)
        .all()
    )

    return {
        "kill_switch": scheduler.status(),
        "queued": [_serialize(t, session) for t in queued],
        "running": [_serialize(t, session) for t in running],
        "needs_you": [_serialize(t, session) for t in needs_you],
        "done": [_serialize(t, session) for t in done],
    }
