from fastapi import Request
from sqlalchemy.orm import Session

from ..models import AuditLog, User


def audit(db: Session, request: Request | None, user: User | None, action: str,
          entity: str | None = None, entity_id: int | None = None, detail: str | None = None) -> None:
    """Queue an audit row on the caller's transaction — it commits (or rolls back) with the change it records."""
    ip = None
    if request is not None:
        fwd = request.headers.get("x-forwarded-for")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    db.add(AuditLog(user_id=user.id if user else None, action=action, entity=entity,
                    entity_id=entity_id, detail=detail, ip=ip))
