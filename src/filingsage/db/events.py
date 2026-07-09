"""Event emission — the pipeline's audit trail (spec §3).

Deliberately does NOT commit: the event joins the caller's open transaction,
so a state change and its event commit atomically or not at all. This is a
lightweight transactional-outbox pattern — an event can never claim something
happened that the database doesn't reflect.
"""

from sqlalchemy.orm import Session

from filingsage.db.models import Event


def emit_event(session: Session, type_: str, entity_id: str, payload: dict | None = None) -> Event:
    event = Event(type=type_, entity_id=entity_id, payload_json=payload or {})
    session.add(event)
    return event