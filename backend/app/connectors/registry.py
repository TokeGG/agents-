from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connector, _utcnow

# Built-in tool names a connector may never shadow (plan Global Constraints).
BUILTIN_TOOL_NAMES: set[str] = {
    "run_shell",
    "read_file",
    "write_file",
    "edit_file",
    "http_get",
    "browse",
    "download_file",
    "ask_user",
    "report_gap",
    "delegate_task",
    "check_subtask",
    "request_connector",
    "register_connector",
    "vault_provision_test_credential",
}


def create_pending(
    session: Session,
    *,
    name: str,
    service_name: str,
    purpose: str,
    parameters_schema: dict,
    source_code: str,
    required_hosts: list[str],
    requested_by_task_id: int | None,
    test_transcript: dict | None,
) -> Connector:
    connector = Connector(
        name=name,
        service_name=service_name,
        purpose=purpose,
        parameters_schema=parameters_schema,
        source_code=source_code,
        required_hosts=required_hosts,
        status="pending_review",
        requested_by_task_id=requested_by_task_id,
        test_transcript=test_transcript,
    )
    session.add(connector)
    session.commit()
    return connector


def find_active(session: Session, service_name: str, purpose: str) -> Connector | None:
    return session.scalars(
        select(Connector).where(
            Connector.service_name == service_name,
            Connector.purpose == purpose,
            Connector.status == "active",
        )
    ).first()


def find_pending(session: Session, service_name: str) -> Connector | None:
    return session.scalars(
        select(Connector).where(
            Connector.service_name == service_name,
            Connector.status == "pending_review",
        )
    ).first()


def list_pending(session: Session) -> list[Connector]:
    return list(
        session.scalars(
            select(Connector).where(Connector.status == "pending_review").order_by(Connector.id)
        ).all()
    )


def list_active(session: Session) -> list[Connector]:
    return list(
        session.scalars(
            select(Connector).where(Connector.status == "active").order_by(Connector.id)
        ).all()
    )


def name_collision(session: Session, name: str, *, exclude_id: int | None = None) -> bool:
    if name in BUILTIN_TOOL_NAMES:
        return True
    query = select(Connector.id).where(Connector.name == name, Connector.status == "active")
    if exclude_id is not None:
        query = query.where(Connector.id != exclude_id)
    return session.scalars(query.limit(1)).first() is not None


def approve(session: Session, connector_id: int, *, approved_by: str) -> Connector:
    connector = session.get(Connector, connector_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    # Validate everything before mutating: a failed approve leaves the row untouched.
    if connector.status != "pending_review":
        raise ValueError(f"connector {connector_id} is {connector.status}, not pending_review")
    if name_collision(session, connector.name, exclude_id=connector.id):
        raise ValueError(
            f"connector name {connector.name!r} collides with a built-in or active connector"
        )
    connector.status = "active"
    connector.approved_at = _utcnow()
    connector.approved_by = approved_by
    session.commit()
    return connector


def reject(session: Session, connector_id: int) -> Connector:
    connector = session.get(Connector, connector_id)
    if connector is None:
        raise ValueError(f"connector {connector_id} not found")
    connector.status = "rejected"
    session.commit()
    return connector
