"""Наряды в календаре «Записи» (22.11.2026, брат): у каждого мастера, кроме записей клиентов, — его несделанные работы
по открытым нарядам. Время берём из «Время на выполнение» (WoWork.minutes, вписывает менеджер); не вписано — подсказка:
нормо-часы × кол-во, у работы «фиксом» — 1 час (показывается «≈»). Нормо-часы — только цена.

Как ставим в график (шаг 1, до очереди менеджера):
  • «в работе» / «запчасти на складе» — сплошной блок от текущего часа;
  • «ждём запчасти» — сплошной блок с дня привоза, если обещают сегодня / завтра / послезавтра; позже или без
    даты — вне графика (отдельный список);
  • согласование, диагностика, подбор — бледный блок «возможно» после сплошных, в свободные окна и загрузку не идёт.
Порядок: «в работе» → «на складе» → «ждём запчасти» (по дню привоза) → по времени приёмки. Записи клиентов — на своих
местах, наряды обтекают их. Рабочие часы и выходные — из настроек «Записи».
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Vehicle, WorkOrder, WoWork, WoWorkSession
from . import settings as st
from .attendance import _aware, local_tz

SOLID = ("work", "instock")
TENTATIVE = ("created", "diag", "approve", "picking")
PARTS_WINDOW = 3          # «ждём запчасти»: в график, если привезут в пределах 3 дней (сегодня, завтра, послезавтра)
HORIZON = 31              # дальше месяца не раскладываем
STATUS_WORD = {"work": "в работе", "instock": "запчасти на складе", "waiting": "ждём запчасти",
               "created": "приёмка", "diag": "диагностика", "approve": "согласование", "picking": "подбор"}


def work_minutes(w: WoWork) -> tuple[int, bool]:
    """(минуты, вписано ли менеджером). Не вписано — нормо-часы × кол-во, у «фикса» — 1 час."""
    if w.minutes:
        return int(w.minutes), True
    if (w.pricing or "hours") == "hours" and w.hours:
        return max(15, int(round(float(w.hours) * float(w.qty or 1) * 60))), False
    return 60, False


@dataclass(eq=False)
class Block:
    wo: WorkOrder
    mech_id: object
    minutes: int
    exact: bool
    kind: str                     # solid / tentative
    earliest: datetime
    rank: tuple
    plate: str = ""
    car: str = ""
    segs: list = field(default_factory=list)   # [(start, end)] — куски по дням
    expected: date | None = None                 # «ждём запчасти»: когда обещают привезти

    @property
    def word(self):
        return STATUS_WORD.get(self.wo.status, self.wo.status)


def _cfg(db, branch_id):
    cfg = st.get(db, branch_id, "schedule")
    return int(cfg.get("day_start", 8)), int(cfg.get("day_end", 19)), set(cfg.get("days_off", [6]) or [])


def _expected(db: Session, wo: WorkOrder):
    """День, когда обещают запчасти по наряду: самая поздняя дата из открытых закупок под наряд (или их строк)."""
    from ..models import Purchase, PurchaseLine
    days = []
    for p in db.scalars(select(Purchase).where(Purchase.work_order_id == wo.id, Purchase.status.in_(("ordered", "partial")))):
        line_days = [l.expected_on for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id))
                     if getattr(l, "expected_on", None) and not l.received]
        days += line_days or ([p.expected_on] if p.expected_on else [])
    return max(days) if days else None


def plan(db: Session, branch_id, masters) -> dict:
    """Раскладка нарядов по мастерам: {"blocks": {mech_id: [Block]}, "off": [Block вне графика], "busy": {...}}."""
    tz = local_tz()
    h1, h2, off_days = _cfg(db, branch_id)
    now = datetime.now(tz)
    today = now.date()
    ids = {m.id for m in masters}
    orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id,
                                                WorkOrder.status.in_(SOLID + TENTATIVE + ("waiting",)))).all()
    if not orders:
        return {"blocks": {i: [] for i in ids}, "off": []}
    works = db.scalars(select(WoWork).where(WoWork.work_order_id.in_([o.id for o in orders]), WoWork.status != "done")).all()
    open_s = {s.wo_work_id: s for s in db.scalars(select(WoWorkSession).where(
        WoWorkSession.wo_work_id.in_([w.id for w in works] or [None]), WoWorkSession.ended_at.is_(None),
        WoWorkSession.kind != "pause"))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))}
    by_order = {}
    for w in works:
        by_order.setdefault(w.work_order_id, []).append(w)
    blocks, off = {i: [] for i in ids}, []
    for o in orders:
        per = {}
        for w in by_order.get(o.id, []):
            mid = w.mechanic_id or o.mechanic_id
            if mid not in ids:
                continue
            mins, exact = work_minutes(w)
            s = open_s.get(w.id)
            if w.status == "doing" and s and s.started_at:   # идёт сейчас — осталось меньше
                mins = max(15, mins - int((now - _aware(s.started_at)).total_seconds() // 60))
            m = per.setdefault(mid, [0, True, w.status == "doing"])
            m[0] += mins
            m[1] = m[1] and exact
            m[2] = m[2] or w.status == "doing"
        v = vehicles.get(o.vehicle_id)
        for mid, (mins, exact, doing) in per.items():
            earliest, kind = now, "solid"
            if o.status in TENTATIVE:
                kind, rank = "tentative", (5, o.created_at)
            elif o.status == "waiting":
                exp = _expected(db, o)
                if exp is None or exp >= today + timedelta(days=PARTS_WINDOW):
                    off.append(Block(o, mid, mins, exact, "off", now, (9,), v.plate if v else "",
                                     f"{v.make} {v.model}" if v else "", expected=exp))
                    continue
                earliest = max(now, datetime.combine(exp, time(h1), tzinfo=tz))
                rank = (3, exp, o.created_at)
            else:
                rank = (0 if (o.status == "work" and doing) else 1 if o.status == "work" else 2, o.created_at)
            blocks[mid].append(Block(o, mid, mins, exact, kind, earliest, rank, v.plate if v else "",
                                     f"{v.make} {v.model}" if v else ""))
    # Записи клиентов — занятые места, наряды их обходят (записи, уже ставшие нарядом, не считаем дважды)
    appts = db.scalars(select(Appointment).where(Appointment.branch_id == branch_id, Appointment.status == "booked",
                                                 Appointment.start_at >= now - timedelta(hours=12),
                                                 Appointment.start_at < now + timedelta(days=HORIZON))).all()
    for mid, lst in blocks.items():
        busy = sorted((_aware(a.start_at).astimezone(tz), _aware(a.start_at).astimezone(tz) + timedelta(minutes=a.duration_min or 30))
                      for a in appts if a.mechanic_id == mid)
        lst.sort(key=lambda b: (b.kind != "solid", b.rank))
        cur = now
        for b in lst:
            cur = _place(b, max(cur, b.earliest), busy, h1, h2, off_days, today)
    return {"blocks": blocks, "off": off}


def _place(b: Block, cur: datetime, busy: list, h1: int, h2: int, off_days: set, today: date) -> datetime:
    """Разложить блок по рабочему времени с cur: пропускаем нерабочие часы, выходные и записи клиентов."""
    tz = local_tz()
    left = b.minutes
    cur = cur.replace(second=0, microsecond=0)
    if cur.minute % 15:
        cur += timedelta(minutes=15 - cur.minute % 15)
    limit = datetime.combine(today + timedelta(days=HORIZON), time(h1), tzinfo=tz)
    while left > 0 and cur < limit:
        d = cur.date()
        day_s, day_e = datetime.combine(d, time(h1), tzinfo=tz), datetime.combine(d, time(min(h2, 23), 59 if h2 >= 24 else 0), tzinfo=tz)
        if d.weekday() in off_days or cur >= day_e:
            cur = datetime.combine(d + timedelta(days=1), time(h1), tzinfo=tz)
            continue
        cur = max(cur, day_s)
        hit = next(((s, e) for s, e in busy if s <= cur < e), None)
        if hit:   # сейчас запись клиента — ждём её конца
            cur = hit[1]
            continue
        nxt = min([s for s, e in busy if s > cur] + [day_e])
        take = min(left, int((nxt - cur).total_seconds() // 60))
        if take <= 0:
            cur = nxt
            continue
        b.segs.append((cur, cur + timedelta(minutes=take)))
        left -= take
        cur += timedelta(minutes=take)
    return cur


def segments_on(p: dict, d: date, solid_only: bool = False) -> dict:
    """{mech_id: [(start, end, Block)]} — куски нарядов в этот день."""
    out = {}
    for mid, lst in p["blocks"].items():
        for b in lst:
            if solid_only and b.kind != "solid":
                continue
            for s, e in b.segs:
                if s.date() == d:
                    out.setdefault(mid, []).append((s, e, b))
    return out


def minutes_by_day(p: dict, mid, solid_only: bool = True) -> dict:
    """{день: минут нарядов} у мастера (для загрузки)."""
    out = {}
    for b in p["blocks"].get(mid, []):
        if solid_only and b.kind != "solid":
            continue
        for s, e in b.segs:
            out[s.date()] = out.get(s.date(), 0) + int((e - s).total_seconds() // 60)
    return out
