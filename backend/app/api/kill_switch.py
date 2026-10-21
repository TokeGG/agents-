from fastapi import APIRouter, Depends

from app.api.tasks import get_scheduler
from app.auth import require_auth
from app.schemas import KillSwitchRequest

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/kill-switch")
async def kill_switch(body: KillSwitchRequest, scheduler=Depends(get_scheduler)):
    return await scheduler.kill_switch(body.mode)
