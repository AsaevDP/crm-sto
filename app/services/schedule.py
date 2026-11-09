"""Запись клиентов: сетка по мастерам, пересечения, переход «приехал → приёмка»."""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment
from . import settings as st
from .attendance import _aware, local_tz


def local(dt: datetime) -> datetime:
    return _aware(dt).astimezone(local_tz())


def to_utc(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in (hhmm or "09:00").split(":")[:2])
    return datetime.combine(d, time(h, m), tzinfo=local_tz()).astimezone(timezone.utc)


def day_range(d1: date, d2: date):
    start = datetime.combine(d1, time.min, tzinfo=local_tz()).astimezone(timezone.utc)
    end = datetime.combine(d2 + timedelta(days=1), time.min, tzinfo=local_tz()).astimezone(timezone.utc)
    return start, end


def between(db: Session, branch_id, d1: date, d2: date, mechanic_id=None, with_cancelled=False) -> list[Appointment]:
    start, end = day_range(d1, d2)
    q = select(Appointment).where(Appointment.branch_id == branch_id, Appointment.start_at >= start,
                                  Appointment.start_at < end)
    if mechanic_id:
        q = q.where(Appointment.mechanic_id == mechanic_id)
    if not with_cancelled:
        q = q.where(Appointment.status != "cancel")
    return db.scalars(q.order_by(Appointment.start_at)).all()


def overlaps(db: Session, a: Appointment) -> list[Appointment]:
    """Другие активные записи к тому же мастеру (или на тот же подъёмник), которые пересекаются по времени."""
    if not a.mechanic_id and not a.lift_id:
        return []
    s = _aware(a.start_at)
    e = s + timedelta(minutes=a.duration_min or 30)
    d = local(s).date()
    out = []
    for b in between(db, a.branch_id, d, d):
        if b.id == a.id or b.status not in ("booked", "arrived"):
            continue
        if not ((a.mechanic_id and b.mechanic_id == a.mechanic_id) or (a.lift_id and b.lift_id == a.lift_id)):
            continue
        bs = _aware(b.start_at)
        if bs < e and s < bs + timedelta(minutes=b.duration_min or 30):
            out.append(b)
    return out


def grid(db: Session, branch_id, d: date, columns: list, appts: list[Appointment]) -> dict:
    """Сетка дня: слоты по шагу, записи с позицией (строка начала, сколько строк) в колонке мастера."""
    cfg = st.get(db, branch_id, "schedule")
    step = max(15, int(cfg.get("step", 30)))
    h1, h2 = int(cfg.get("day_start", 8)), int(cfg.get("day_end", 19))
    # Если запись выходит за рабочие часы — расширяем сетку, чтобы её было видно
    for a in appts:
        la = local(a.start_at)
        h1 = min(h1, la.hour)
        h2 = max(h2, (la + timedelta(minutes=a.duration_min or step)).hour + (1 if (la + timedelta(minutes=a.duration_min or step)).minute else 0))
    h2 = min(max(h2, h1 + 1), 24)
    slots = [f"{(h1 * 60 + i * step) // 60:02d}:{(h1 * 60 + i * step) % 60:02d}"
             for i in range((h2 - h1) * 60 // step)]
    col_idx = {c["id"]: i for i, c in enumerate(columns)}
    blocks = []
    for a in appts:
        la = local(a.start_at)
        row = ((la.hour - h1) * 60 + la.minute) // step
        span = max(1, -(-(a.duration_min or step) // step))
        col = col_idx.get(a.mechanic_id, col_idx.get(None))
        if col is None:
            continue
        blocks.append({"a": a, "row": row + 2, "span": span, "col": col + 2, "time": la.strftime("%H:%M"),
                       "end": (la + timedelta(minutes=a.duration_min or step)).strftime("%H:%M")})
    return {"slots": slots, "blocks": blocks, "step": step}
