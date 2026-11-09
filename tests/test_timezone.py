"""Время (10.11.2026): из базы приходит с поясом (UTC), на экранах — местное время, а не UTC."""
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog

from .test_step6 import _as


def test_times_are_utc_aware_and_shown_local(client):
    _as(client, "admin")
    with SessionLocal() as db:
        r = db.scalar(select(AuditLog).order_by(AuditLog.created_at.desc()))
    assert r.created_at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - r.created_at).total_seconds()) < 120

    # Журнал показывает вход по местным часам
    page = client.get(f"/audit?user={r.record_id}&action=login").text
    assert r.created_at.astimezone().strftime("%H:%M") in page
