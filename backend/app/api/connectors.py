import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.connectors import registry
from app.db import get_session

router = APIRouter(prefix="/connectors", dependencies=[Depends(require_auth)])


def _allowed_egress_hosts() -> set[str]:
    raw = os.environ.get("SANDBOX_EGRESS_ALLOWLIST", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


@router.get("/pending")
def list_pending(session: Session = Depends(get_session)):
    allowed = _allowed_egress_hosts()
    rows = []
    for connector in registry.list_pending(session):
        missing = [host for host in connector.required_hosts if host.strip().lower() not in allowed]
        rows.append(
            {
                "id": connector.id,
                "name": connector.name,
                "service_name": connector.service_name,
                "purpose": connector.purpose,
                "source_code": connector.source_code,
                "test_transcript": connector.test_transcript,
                "required_hosts": connector.required_hosts,
                "missing_egress_hosts": missing,
            }
        )
    return rows


@router.post("/{connector_id}/approve")
def approve(connector_id: int, session: Session = Depends(get_session)):
    try:
        connector = registry.approve(session, connector_id, approved_by="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": connector.id, "name": connector.name, "status": connector.status}


@router.post("/{connector_id}/reject")
def reject(connector_id: int, session: Session = Depends(get_session)):
    try:
        connector = registry.reject(session, connector_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": connector.id, "status": connector.status}
