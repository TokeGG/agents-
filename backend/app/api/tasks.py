import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.agent.model_adapter import ModelAdapter, OpenAICompatAdapter
from app.agent.tools import (
    ToolRegistry,
    ask_user_tool,
    browse_tool,
    check_subtask_tool,
    delegate_task_tool,
    download_file_tool,
    edit_file_tool,
    http_get_tool,
    read_file_tool,
    register_connector_tool,
    report_gap_tool,
    request_connector_tool,
    run_shell_tool,
    vault_provision_test_credential_tool,
    write_file_tool,
)
from app.connectors.registry import list_active
from app.connectors.dynamic_tool import DynamicConnectorTool
from app.db import get_session
from app.models import Event, Rating, Task
from app.agent.tools import ToolContext
from app.browser.service import BrowserService
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import DockerContainerRuntime
from app.vault import Vault
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


def get_reviewer_adapter() -> ModelAdapter:
    """Same shape as get_adapter(), but for the (optional) stronger reviewer
    tier model_tiering escalates to. Falls back to the worker adapter's env
    vars when the REVIEWER_* ones are unset, so an operator who hasn't
    configured a separate reviewer model gets the additive-cost-free default
    (worker == reviewer) instead of a crash."""
    return OpenAICompatAdapter(
        base_url=os.environ.get("REVIEWER_MODEL_API_BASE", os.environ.get("MODEL_API_BASE", "")),
        api_key=os.environ.get("REVIEWER_MODEL_API_KEY", os.environ.get("MODEL_API_KEY", "")),
        model_name=os.environ.get("REVIEWER_MODEL_NAME", os.environ.get("MODEL_NAME", "default")),
    )


def get_registry(session: Session = Depends(get_session)) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(http_get_tool)
    registry.register(ask_user_tool)
    registry.register(report_gap_tool)
    registry.register(delegate_task_tool)
    registry.register(check_subtask_tool)
    registry.register(run_shell_tool)
    registry.register(read_file_tool)
    registry.register(write_file_tool)
    registry.register(edit_file_tool)
    registry.register(browse_tool)
    registry.register(download_file_tool)
    registry.register(request_connector_tool)
    registry.register(register_connector_tool)
    registry.register(vault_provision_test_credential_tool)
    for connector in list_active(session):
        registry.register(DynamicConnectorTool(connector))
    return registry


def get_sandbox_manager(session_factory) -> SandboxManager:
    """Real (non-test) SandboxManager, backed by Docker. Built lazily by
    callers (lifespan wiring) so tests can override this via
    app.dependency_overrides with a fake, same lazy-override pattern as
    get_adapter/get_registry."""
    return SandboxManager(DockerContainerRuntime(), session_factory=session_factory)


def get_browser_service() -> BrowserService:
    """Real (non-test) BrowserService. Built lazily by callers (lifespan
    wiring) so tests can override this via app.dependency_overrides with a
    fake, same lazy-override pattern as get_sandbox_manager/get_registry."""
    return BrowserService()


def get_vault(session_factory) -> Vault:
    """Real (non-test) Vault, backed by VAULT_ENCRYPTION_KEY. Built lazily by
    callers (lifespan wiring) so tests can override this via
    app.dependency_overrides with a fake, same lazy-override pattern as
    get_sandbox_manager/get_browser_service. Fails closed (raises) if the key
    is unset, matching validate_sandbox_egress_config's precedent."""
    key = os.environ.get("VAULT_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("VAULT_ENCRYPTION_KEY environment variable is not set")
    return Vault(key.encode(), session_factory)


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
            ctx = ToolContext(
                task_id=task_id,
                session=session,
                scheduler=scheduler,
                sandbox_manager=getattr(scheduler, "sandbox_manager", None),
                browser_service=getattr(scheduler, "browser_service", None),
                vault=getattr(scheduler, "vault", None),
            )
            try:
                result = tool.run(arguments, ctx)
            except Exception as exc:  # noqa: BLE001 - mirrors run_task_loop's own
                # generic tool-call-failure except clause: anything the tool
                # call can raise (SandboxSlotUnavailable, a RuntimeError from
                # run_shell against a stopped container, httpx.HTTPError, bad
                # arguments, etc.) resolves to a normal failure result instead
                # of an uncaught 500 that leaves the task stuck in
                # awaiting_approval with no event recorded.
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


@router.post("/tasks/{task_id}/kill")
async def kill_task(
    task_id: int,
    session: Session = Depends(get_session),
    scheduler=Depends(get_scheduler),
):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    await scheduler.kill_task(task_id)
    session.expire(task)
    return {"id": task.id, "status": task.status}
