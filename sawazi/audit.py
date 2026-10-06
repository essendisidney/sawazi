"""Audit log: every manual action, recorded with who, when, before and after.

`record()` adds the event to the caller's session without committing, so it is
saved in the same transaction as the change it describes: both land or neither.

Events are append-only. The ORM refuses to update or delete them; in production
the database role used by the app should also lack UPDATE/DELETE on audit_events.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from .auth import Principal, utcnow
from .models import AuditEvent

# Never written to the log, at any depth, whatever the caller passes.
_SECRET_FIELDS = {"password", "password_hash", "new_password", "current_password", "key", "key_hash", "token",
                  "token_hash"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # datetimes and the like


def platform(institution_id: int) -> Principal:
    """The Pesara operator, acting with the platform key."""
    return Principal("platform", None, institution_id, "platform", "Pesara platform")


def anonymous(institution_id: int) -> Principal:
    """Someone not (yet) identified, e.g. a failed login attempt against one of this institution's accounts."""
    return Principal("anonymous", None, institution_id, "", "unknown")


def record(s: Session, who: Principal, action: str, entity_type: str | None = None, entity_id: int | None = None, *,
           before: dict | None = None, after: dict | None = None, note: str | None = None,
           ip: str | None = None) -> AuditEvent:
    ev = AuditEvent(institution_id=who.institution_id, at=utcnow(), actor_kind=who.kind, actor_id=who.id,
                    actor_name=who.name, action=action, entity_type=entity_type, entity_id=entity_id,
                    before=_clean(before), after=_clean(after), note=note, ip=ip)
    s.add(ev)
    return ev


def _append_only(*_args):
    raise RuntimeError("audit events are append-only")


event.listen(AuditEvent, "before_update", _append_only)
event.listen(AuditEvent, "before_delete", _append_only)
