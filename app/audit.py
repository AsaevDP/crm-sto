"""Автоматический журнал действий.

Перед каждой записью в БД смотрим, какие строки создаются, меняются и удаляются,
и пишем это в audit_log. Тут же проставляем created_by и branch_id,
чтобы не делать это вручную в каждом экране.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from .db import Entity, current_branch_id, current_user_id
from .models import AuditLog

# Эти поля в журнал не пишем (только отметку, что поменялись)
SECRET_FIELDS = {"password_hash", "pin_hash"}
SKIP_FIELDS = {"updated_at", "created_at"}


def _val(v):
    if isinstance(v, (uuid.UUID, Decimal)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _snapshot(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.key in SKIP_FIELDS:
            continue
        v = getattr(obj, col.key, None)
        out[col.key] = "•••" if col.key in SECRET_FIELDS and v else _val(v)
    return out


def write(session: Session, table: str, record_id, action: str, changes: dict | None = None):
    """Ручная запись в журнал (вход/выход и т.п.)."""
    session.add(AuditLog(table=table, record_id=str(record_id), action=action, changes=changes or {}))


@event.listens_for(Session, "before_flush")
def _before_flush(session: Session, flush_context, instances):
    uid = current_user_id.get()
    bid = current_branch_id.get()
    logs = []

    for obj in list(session.new):
        if not isinstance(obj, Entity):
            continue
        if obj.id is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_by", None) is None and uid:
            obj.created_by = uid
        if getattr(obj, "branch_id", None) is None and bid:
            obj.branch_id = bid
        if isinstance(obj, AuditLog):
            continue
        logs.append(AuditLog(table=obj.__tablename__, record_id=str(obj.id), action="create",
                             changes=_snapshot(obj)))

    for obj in list(session.dirty):
        if not isinstance(obj, Entity) or isinstance(obj, AuditLog) or not session.is_modified(obj):
            continue
        diff = {}
        for attr in inspect(obj).attrs:
            if attr.key in SKIP_FIELDS or not hasattr(obj.__table__.c, attr.key):
                continue
            h = attr.history
            if h.has_changes():
                old = h.deleted[0] if h.deleted else None
                new = h.added[0] if h.added else None
                if attr.key in SECRET_FIELDS:
                    diff[attr.key] = ["•••", "••• (изменён)"]
                else:
                    diff[attr.key] = [_val(old), _val(new)]
        if diff:
            logs.append(AuditLog(table=obj.__tablename__, record_id=str(obj.id), action="update", changes=diff))

    for obj in list(session.deleted):
        if isinstance(obj, Entity) and not isinstance(obj, AuditLog):
            logs.append(AuditLog(table=obj.__tablename__, record_id=str(obj.id), action="delete",
                                 changes=_snapshot(obj)))

    for log in logs:
        log.id = uuid.uuid4()
        log.created_by = uid
        log.branch_id = bid
        session.add(log)
