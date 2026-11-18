"""Мойка (18.11.2026, брат): прайс, быстрая приёмка, живая очередь с ожидаемым временем.

Быстрый наряд мойки (WorkOrder.kind="wash"): клиент, машина, класс машины, услуги из прайса (сразу работы с ценой),
кто моет (необязательно) и бокс (необязательно). Без диагностики, согласования, запчастей и подъёмника. Наряд сразу
«В работе» → мойщик «Начал / Выполнено» → «Готово» → оплата → «Выдать авто» (как у всех: только после полной оплаты).

Очередь: машины, которые ещё не начали мыть, — по времени приезда; менеджер может поднять машину «↑» (queue_rank).
Ожидаемое время: каждый бокс — своя дорожка; машина без бокса встаёт туда, где освободится раньше. Длительность —
из прайса (WoWork.minutes)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import User, Vehicle, WorkOrder, WoWork, WoWorkSession
from ..models.orders import WO_ACTIVE
from ..models.wash import WashBox, WashService
from .orders import activity_label

CLASSES = {"car": "Легковая", "suv": "Кроссовер", "bus": "Бус"}
KINDS = {"wash": "Мойка", "detail": "Детейлинг"}

# Прайс по умолчанию — обычные услуги моек; цены примерные, менеджер правит в «Мойка → Прайс»
DEFAULTS = [
    # название, вид, легковая, кроссовер, бус, минут
    ("Экспресс-мойка (поверхностная)", "wash", 200, 250, 300, 15),
    ("Мойка кузова (бесконтактная)", "wash", 250, 300, 400, 20),
    ("Мойка кузова ручная", "wash", 350, 400, 500, 30),
    ("Комплексная мойка (кузов + салон)", "wash", 600, 700, 900, 60),
    ("Пылесос салона", "wash", 200, 250, 300, 20),
    ("Влажная уборка салона", "wash", 250, 300, 350, 25),
    ("Стёкла изнутри", "wash", 100, 100, 150, 10),
    ("Мойка дисков", "wash", 150, 150, 200, 15),
    ("Чернение шин", "wash", 100, 100, 150, 10),
    ("Чернение дисков", "wash", 200, 200, 250, 20),
    ("Мойка ковриков", "wash", 50, 50, 80, 5),
    ("Мойка двигателя", "wash", 400, 450, 500, 30),
    ("Быстрый воск", "wash", 200, 250, 300, 15),
    ("Химчистка салона", "detail", 3000, 3500, 4500, 240),
    ("Полировка кузова", "detail", 4000, 5000, 6000, 360),
    ("Озонирование салона", "detail", 500, 500, 600, 30),
]


def ensure_defaults(db: Session, branch_id) -> None:
    """Прайс пустой (первый запуск) — заводим обычные услуги. Если хоть одна услуга есть — ничего не трогаем."""
    if db.scalar(select(WashService.id).where(WashService.branch_id == branch_id).limit(1)):
        return
    from ..db import current_branch_id
    current_branch_id.set(branch_id)
    for i, (name, kind, a, b, c, m) in enumerate(DEFAULTS):
        db.add(WashService(branch_id=branch_id, name=name, kind=kind, price_car=Decimal(a), price_suv=Decimal(b),
                           price_bus=Decimal(c), minutes=m, sort=(i + 1) * 10))
    db.flush()


def services(db: Session, branch_id, active_only: bool = False) -> list[WashService]:
    ensure_defaults(db, branch_id)
    q = select(WashService).where(WashService.branch_id == branch_id)
    if active_only:
        q = q.where(WashService.active.is_(True))
    return db.scalars(q.order_by(WashService.active.desc(), WashService.kind.desc(), WashService.sort, WashService.name)).all()


def price(s: WashService, car_class: str) -> Decimal:
    return Decimal({"suv": s.price_suv, "bus": s.price_bus}.get(car_class, s.price_car) or 0)


def boxes(db: Session, branch_id, active_only: bool = True) -> list[WashBox]:
    q = select(WashBox).where(WashBox.branch_id == branch_id)
    if active_only:
        q = q.where(WashBox.active.is_(True))
    return db.scalars(q.order_by(WashBox.number)).all()


def fmt_min(m: int) -> str:
    m = int(m or 0)
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч" + (f" {m % 60:02d} мин" if m % 60 else "")


# ---------- Очередь ----------
@dataclass
class Car:
    o: WorkOrder
    v: Vehicle | None
    works: list
    stage: str                       # queue — ждёт, doing — моют, done — готово (ждёт оплаты / клиента)
    minutes: int = 0                 # всего по прайсу
    left: int = 0                    # осталось (примерно)
    box: WashBox | None = None
    who: list = field(default_factory=list)
    eta_start: datetime | None = None
    eta_end: datetime | None = None
    pos: int = 0                     # место в очереди (1 — следующая)
    act: str = ""                    # «изм. 14:32 · Литвиненко» — последнее действие


def _stage(o: WorkOrder, works) -> str:
    if o.status in ("ready", "paid") or (works and all(w.status == "done" for w in works)):
        return "done"
    if any(w.status in ("doing", "paused", "done") for w in works):
        return "doing"
    return "queue"


def _sort_key(o: WorkOrder):
    return (o.queue_rank if o.queue_rank is not None else 10 ** 9, o.created_at)


def board(db: Session, branch_id) -> dict:
    """Всё для экрана очереди: queue — ждут (по порядку, с «≈ начнут / ≈ готово»), doing — моют, done — готовы."""
    orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.kind == "wash",
                                                WorkOrder.status.in_(WO_ACTIVE))).all()
    ids = [o.id for o in orders]
    works = {}
    for w in db.scalars(select(WoWork).where(WoWork.work_order_id.in_(ids)).order_by(WoWork.created_at)) if ids else []:
        works.setdefault(w.work_order_id, []).append(w)
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))} if ids else {}
    all_boxes = {b.id: b for b in boxes(db, branch_id, active_only=False)}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == branch_id))}
    open_s = {}
    for s in db.scalars(select(WoWorkSession).where(WoWorkSession.work_order_id.in_(ids), WoWorkSession.ended_at.is_(None),
                                                    WoWorkSession.kind != "pause")) if ids else []:
        open_s[s.wo_work_id] = s
    t = now()
    cars = []
    for o in orders:
        ws = works.get(o.id, [])
        total = sum(int(w.minutes or 0) for w in ws)
        left = 0
        for w in ws:
            if w.status == "done":
                continue
            m = int(w.minutes or 0)
            s = open_s.get(w.id)
            if w.status == "doing" and s and s.started_at:
                from .attendance import _aware
                m = max(m - int((t - _aware(s.started_at)).total_seconds() // 60), 5 if m else 0)
            left += m
        who_ids = list(dict.fromkeys(i for i in [*(w.mechanic_id for w in ws), o.mechanic_id] if i))
        cars.append(Car(o=o, v=vehicles.get(o.vehicle_id), works=ws, stage=_stage(o, ws), minutes=total, left=left,
                        box=all_boxes.get(o.wash_box_id), who=[people[i] for i in who_ids if i in people],
                        act=activity_label(o, people.get(o.activity_by))))
    queue = sorted([c for c in cars if c.stage == "queue"], key=lambda c: _sort_key(c.o))
    # 18.11.2026: «Моют» и «Готовы» — свежие сверху (последнее действие); очередь — по приезду (дата приёмки)
    from .orders import fresh_key
    doing = sorted([c for c in cars if c.stage == "doing"], key=lambda c: fresh_key(c.o), reverse=True)
    done = sorted([c for c in cars if c.stage == "done"], key=lambda c: fresh_key(c.o), reverse=True)
    # Дорожки: каждый работающий бокс; боксов нет — одна общая
    lanes = {b.id: t for b in all_boxes.values() if b.active} or {None: t}

    def lane_for(c: Car):
        if c.o.wash_box_id in lanes:
            return c.o.wash_box_id
        return min(lanes, key=lambda k: lanes[k])

    for c in doing:
        k = lane_for(c)
        c.eta_end = t + timedelta(minutes=c.left)
        lanes[k] = max(lanes[k], c.eta_end)
    for i, c in enumerate(queue, 1):
        k = lane_for(c)
        c.pos = i
        c.eta_start = max(lanes[k], t)
        c.eta_end = c.eta_start + timedelta(minutes=c.minutes)
        lanes[k] = c.eta_end
    return {"queue": queue, "doing": doing, "done": done, "boxes": [b for b in all_boxes.values() if b.active],
            "n_lanes": len(lanes)}


def raise_up(db: Session, branch_id, wo: WorkOrder) -> bool:
    """«↑» — поднять машину на одно место в очереди (клиент спешит, постоянный)."""
    q = board(db, branch_id)["queue"]
    order = [c.o for c in q]
    if wo not in order:
        return False
    i = order.index(wo)
    if i == 0:
        return False
    order[i - 1], order[i] = order[i], order[i - 1]
    for n, o in enumerate(order, 1):
        o.queue_rank = n
    return True


def can_take(user: User, c: Car) -> bool:
    """Мойщик может взять машину: на неё никто не назначен или назначен он сам."""
    ids = {w.mechanic_id for w in c.works if w.mechanic_id} | ({c.o.mechanic_id} if c.o.mechanic_id else set())
    return not ids or user.id in ids


def take(db: Session, wo: WorkOrder, user: User) -> WoWork | None:
    """Мойщик взял машину: он — исполнитель (своих несделанных работ без исполнителя), первая работа — «Начал»."""
    if not wo.mechanic_id:
        wo.mechanic_id = user.id
    first = None
    for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)):
        if w.status != "done" and not w.mechanic_id:
            w.mechanic_id = user.id
        if first is None and w.status in ("todo", "paused") and (w.mechanic_id or wo.mechanic_id) == user.id:
            first = w
    if first:
        t = now()
        for s in db.scalars(select(WoWorkSession).where(WoWorkSession.wo_work_id == first.id, WoWorkSession.ended_at.is_(None))):
            s.ended_at = t
        db.add(WoWorkSession(work_order_id=wo.id, wo_work_id=first.id, mechanic_id=user.id, lift_id=None, started_at=t))
        first.status = "doing"
    wo.queue_rank = None
    from .orders import event
    event(db, wo, "status", f"Мойка: взял {user.full_name}")
    return first


def next_for(db: Session, branch_id, user: User) -> WorkOrder | None:
    """Следующая машина для мойщика: первая в очереди, которую он может взять."""
    for c in board(db, branch_id)["queue"]:
        if can_take(user, c):
            return c.o
    return None


def is_wash_order(wo: WorkOrder) -> bool:
    return (wo.kind or "") == "wash"
