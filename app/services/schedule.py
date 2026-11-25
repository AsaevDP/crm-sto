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
                       "end": (la + timedelta(minutes=a.duration_min or step)).strftime("%H:%M"),
                       "mech": a.mechanic_id})
    # Красная линия «сейчас» — только для сегодняшнего дня и в пределах сетки
    now_px = None
    if d == date.today():
        n = datetime.now(local_tz())
        mins = (n.hour - h1) * 60 + n.minute
        if 0 <= mins <= (h2 - h1) * 60:
            now_px = round(54 + mins / step * 30)   # 54 — высота шапки с мастерами, 30 — строка сетки
    return {"slots": slots, "blocks": blocks, "step": step, "h1": h1, "h2": h2, "now_px": now_px}


# Цвет мастера в записи: у каждого свой (по порядку), чтобы колонки и записи различались с первого взгляда
MECH_COLORS = ["blue", "orange", "purple", "teal", "indigo", "green", "red", "yellow", "graphite"]


def mech_colors(mechanics) -> dict:
    return {m.id: MECH_COLORS[i % len(MECH_COLORS)] for i, m in enumerate(mechanics)}


def period_load(db: Session, branch_id, d1: date, d2: date, mechanics, appts: list[Appointment], extra: dict | None = None) -> dict:
    """Загрузка мастеров за период (11.11.2026): по каждому — записей, часов записано, часов работы
    (рабочие дни × часы дня из настроек), % и минуты по дням (для тепловой карты месяца)."""
    cfg = st.get(db, branch_id, "schedule")
    day_h = max(1, int(cfg.get("day_end", 19)) - int(cfg.get("day_start", 8)))
    off = set(cfg.get("days_off", [5, 6]) or [])
    n_days = sum(1 for i in range((d2 - d1).days + 1) if (d1 + timedelta(days=i)).weekday() not in off)
    cap = max(1, n_days) * day_h
    out = {}
    for m in mechanics:
        mine = [a for a in appts if a.mechanic_id == m.id and a.status in ("booked", "arrived")]
        by_day = {}
        for a in mine:
            k = local(a.start_at).date()
            by_day[k] = by_day.get(k, 0) + (a.duration_min or 30)
        rec_h = sum(by_day.values()) / 60
        # 22.11.2026 (брат): + несделанные работы по нарядам (workload.minutes_by_day) — время мастера занято и ими
        wo_days = {k: v for k, v in (extra or {}).get(m.id, {}).items() if d1 <= k <= d2}
        for k, v in wo_days.items():
            by_day[k] = by_day.get(k, 0) + v
        hours = sum(by_day.values()) / 60
        out[m.id] = {"n": len(mine), "hours": hours, "cap": cap, "pct": min(100, round(hours * 100 / cap)),
                     "rec_h": rec_h, "wo_h": sum(wo_days.values()) / 60,
                     "by_day": by_day, "noshow": sum(1 for a in appts if a.mechanic_id == m.id and a.status == "noshow")}
    out["_day_h"] = day_h
    out["_off"] = off
    return out


def _busy(appts) -> list[tuple[datetime, datetime]]:
    out = []
    for a in appts:
        if a.status in ("booked", "arrived"):
            s = local(a.start_at)
            out.append((s, s + timedelta(minutes=a.duration_min or 30)))
    return sorted(out)


def day_load(db: Session, branch_id, d: date, columns: list, appts: list[Appointment], min_gap: int = 60,
             extra: dict | None = None) -> dict:
    """По каждому мастеру: занято минут, % загрузки рабочего дня, свободные окна (не короче min_gap минут)."""
    cfg = st.get(db, branch_id, "schedule")
    h1, h2 = int(cfg.get("day_start", 8)), int(cfg.get("day_end", 19))
    tz = local_tz()
    start = datetime.combine(d, time(h1), tzinfo=tz)
    end = datetime.combine(d, time(min(h2, 23), 59 if h2 >= 24 else 0), tzinfo=tz)
    work = max(1, int((end - start).total_seconds() // 60))
    nowl = datetime.now(tz)
    out = {}
    for c in columns:
        mine = [a for a in appts if a.mechanic_id == c["id"]]
        busy = sorted(_busy(mine) + [(s, e) for s, e, *_ in (extra or {}).get(c["id"], [])])   # + наряды (22.11.2026)
        booked = sum(int((min(e, end) - max(s, start)).total_seconds() // 60) for s, e in busy if e > start and s < end)
        free, cur = [], max(start, nowl) if d == nowl.date() else start
        if d < nowl.date():
            cur = end
        cur = cur.replace(second=0, microsecond=0)
        if cur.minute % 15:  # окна — от ближайшей четверти часа
            cur += timedelta(minutes=15 - cur.minute % 15)
        for s, e in busy + [(end, end)]:
            if s > cur and (s - cur) >= timedelta(minutes=min_gap):
                free.append((cur.strftime("%H:%M"), min(s, end).strftime("%H:%M"), int((min(s, end) - cur).total_seconds() // 60)))
            cur = max(cur, e)
        out[c["id"]] = {"n": len([a for a in mine if a.status != "cancel"]), "booked": booked,
                        "pct": min(100, round(booked * 100 / work)), "free": free}
    return out


def reminders(db: Session, branch_id) -> dict:
    """«Напомнить о записи» (20.11.2026, брат).
    late  — 🔴 сегодня: время прошло, «Приехал» не отмечен (позвонить: едет ли);
    call  — 🟠 завтра: позвонить сегодня, напомнить;
    done  — уже позвонили (подтвердил), для сведения.
    Позвонил и клиент подтвердил — запись из счёта уходит (called_at сегодня)."""
    now = datetime.now(timezone.utc)
    today = date.today()
    start_today, _ = day_range(today, today)
    out = {"late": [], "call": [], "done": [], "here": []}
    here_v, here_p = _cars_here(db, branch_id)
    for a in between(db, branch_id, today, today + timedelta(days=1)):
        if a.status != "booked":
            continue
        called = a.called_at is not None and _aware(a.called_at) >= start_today
        is_today = local(a.start_at).date() == today
        if is_today and _aware(a.start_at) + LATE_AFTER > now:
            continue   # 20.11.2026: время ещё не пришло или прошло меньше 15 минут — ждём
        if is_today and ((a.vehicle_id and a.vehicle_id in here_v) or (_plate(a.plate) and _plate(a.plate) in here_p)):
            out["here"].append(a)   # машина клиента уже у нас (открытый наряд) — звонить «едет ли» незачем
            continue
        (out["done"] if called else out["late"] if is_today else out["call"]).append(a)
    return out


LATE_AFTER = timedelta(minutes=15)   # «не приехал» — через 15 минут после времени записи


# ---------- 21.11.2026 (брат): сигналы мастеру в «Записи» ----------
# 🟣 new     — к нему записали (или перенесли / передали ему) — пока не нажал «✓ Видел»;
# 🟠 wait    — сегодня ждём (записан, ещё не приехал);
# 🟢 arrived — машина приехала (наряд из записи), а он в этом наряде ещё не нажал «Начал»;
# 🔴 noshow  — менеджер отметил «не приехал» — пока не нажал «✓ Видел».
SIGNAL_DAYS = 14   # приехали / не приехали — смотрим за две недели назад


def seen_key(a: Appointment) -> str:
    """Что мастер видел: мастер, время, статус. Изменилось что-то из этого — запись снова «новая» для него."""
    return f"{a.mechanic_id}|{local(a.start_at):%Y%m%d%H%M}|{a.status}"


def is_seen(a: Appointment) -> bool:
    return bool(a.seen_key) and a.seen_key == seen_key(a)


def mark_seen(a: Appointment):
    from ..db import now
    a.seen_key, a.seen_at = seen_key(a), now()


def master_signals(db: Session, branch_id, user_id) -> dict:
    from ..models import WorkOrder, WoWorkSession
    from ..models.orders import WO_ACTIVE
    today = date.today()
    start_today, _ = day_range(today, today)
    q = select(Appointment).where(Appointment.branch_id == branch_id, Appointment.mechanic_id == user_id,
                                  Appointment.status.in_(("booked", "arrived", "noshow")),
                                  Appointment.start_at >= start_today - timedelta(days=SIGNAL_DAYS))
    out = {"new": [], "wait": [], "arrived": [], "noshow": []}
    for a in db.scalars(q.order_by(Appointment.start_at)):
        d = local(a.start_at).date()
        if a.status == "booked" and d >= today:
            if not is_seen(a):
                out["new"].append(a)
            if d == today:
                out["wait"].append(a)
        elif a.status == "noshow" and not is_seen(a):
            out["noshow"].append(a)
        elif a.status == "arrived" and a.work_order_id:
            wo = db.get(WorkOrder, a.work_order_id)
            if not wo or wo.status not in WO_ACTIVE or wo.intake_pending:   # 25.11: приёмку ещё оформляют
                continue
            began = db.scalar(select(WoWorkSession.id).where(WoWorkSession.work_order_id == wo.id,
                                                             WoWorkSession.mechanic_id == user_id).limit(1))
            if began is None:
                out["arrived"].append(a)
    return out


def _plate(p: str) -> str:
    return "".join(ch for ch in (p or "").upper() if ch.isalnum())


def _cars_here(db: Session, branch_id) -> tuple[set, set]:
    """Машины, которые сейчас у нас: открытые наряды (id машины и госномер без пробелов)."""
    from ..models import Vehicle, WorkOrder
    rows = db.execute(select(Vehicle.id, Vehicle.plate).join(WorkOrder, WorkOrder.vehicle_id == Vehicle.id).where(
        WorkOrder.branch_id == branch_id, WorkOrder.status.not_in(("closed", "cancel", "cancelled")))).all()
    return {r[0] for r in rows}, {_plate(r[1]) for r in rows if r[1]}
