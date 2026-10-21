import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import require_auth_ws
from app.db import get_session
from app.models import Event

router = APIRouter()

POLL_INTERVAL_SECONDS = 0.2


@router.websocket("/tasks/{task_id}/stream")
async def stream_task_events(
    websocket: WebSocket,
    task_id: int,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_auth_ws),
):
    await websocket.accept()
    last_id_sent = 0
    try:
        while True:
            events = (
                session.query(Event)
                .filter_by(task_id=task_id)
                .filter(Event.id > last_id_sent)
                .order_by(Event.id)
                .all()
            )
            for event in events:
                await websocket.send_json({"type": event.type, "payload": event.payload})
                last_id_sent = event.id
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        return
