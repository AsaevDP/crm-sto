"""Передача наряда и несколько мастеров на машине (13.11.2026, брат).

Передать можно менеджера, ведущего мастера (пост = мастер: за каждым постом закреплён свой) и запчастиста — без
подтверждения директора. Новому — окошко «Вам передан наряд» и красная цифра «Мне передали», пока не нажал «✓ Принял».
Прежний менеджер ещё 3 дня видит наряд в «Переданных» — только посмотреть; вернуть может только новый.
Ещё мастера (электрик, моторист, мойщик…) — OrderMaster: у каждого свои проблемы и своя «Диагностика готова»,
на согласование наряд уходит, когда закончили все.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import OrderHandover, OrderMaster, User, WorkOrder, WoWork
from ..models.orders import HANDOVER_KIND

RO_DAYS = 3   # сколько дней прежний менеджер видит переданный наряд


# ---------- Мастера наряда ----------
def extra_masters(db: Session, wo: WorkOrder) -> list[OrderMaster]:
    return list(db.scalars(select(OrderMaster).where(OrderMaster.work_order_id == wo.id, OrderMaster.active.is_(True))
                           .order_by(OrderMaster.created_at)))


def extra_ids(db: Session, wo: WorkOrder) -> list:
    return [m.user_id for m in extra_masters(db, wo) if m.user_id != wo.mechanic_id]


def is_extra(db: Session, wo: WorkOrder, uid) -> OrderMaster | None:
    return db.scalar(select(OrderMaster).where(OrderMaster.work_order_id == wo.id, OrderMaster.user_id == uid,
                                               OrderMaster.active.is_(True)))


def add_master(db: Session, wo: WorkOrder, user: User, work_name: str = "", by=None, tell: bool = True) -> str:
    """Ещё один мастер на машину. Что делает — его работа в наряде (цену поставит менеджер). Ошибка или ''."""
    if not user or user.id == wo.mechanic_id:
        return "Этот мастер уже ведёт машину"
    if is_extra(db, wo, user.id):
        return f"{user.full_name} уже работает с машиной"
    db.add(OrderMaster(work_order_id=wo.id, user_id=user.id))
    if work_name.strip():
        from .orders import _hour_rate
        db.add(WoWork(work_order_id=wo.id, name=work_name.strip()[:200], mechanic_id=user.id, pricing="hours",
                      rate=_hour_rate(db, wo.branch_id)))
    db.flush()
    if tell:
        record(db, wo, "master", None, user.id, by)
    return ""


def diag_state(db: Session, wo: WorkOrder) -> list[dict]:
    """Кто диагностирует и готов ли: ведущий + остальные мастера (менеджер видит по каждому)."""
    out = []
    ids = [wo.mechanic_id] if wo.mechanic_id else []
    people = {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids + [m.user_id for m in extra_masters(db, wo)])))}
    if wo.mechanic_id:
        out.append({"user": people.get(wo.mechanic_id), "lead": True, "done": wo.lead_diag_at})
    for m in extra_masters(db, wo):
        if m.user_id != wo.mechanic_id:
            out.append({"user": people.get(m.user_id), "lead": False, "done": m.diag_done_at, "m": m})
    return out


def mark_diag_done(db: Session, wo: WorkOrder, uid) -> bool:
    """Мастер закончил свою диагностику. True — закончили все (наряд можно отправлять на согласование)."""
    if uid == wo.mechanic_id:
        wo.lead_diag_at = now()
    else:
        m = is_extra(db, wo, uid)
        if m:
            m.diag_done_at = now()
    db.flush()
    return all(s["done"] for s in diag_state(db, wo))


# ---------- Передача ----------
def record(db: Session, wo: WorkOrder, kind: str, from_id, to_id, by=None):
    """Запись о передаче + окошко новому. Старые неподтверждённые передачи этой роли — закрываем (передали дальше)."""
    if not to_id:
        return
    for h in db.scalars(select(OrderHandover).where(OrderHandover.work_order_id == wo.id, OrderHandover.kind == kind,
                                                    OrderHandover.accepted_at.is_(None), OrderHandover.to_id != to_id)):
        if kind != "master":
            h.accepted_at = now()
    db.add(OrderHandover(work_order_id=wo.id, kind=kind, from_id=from_id, to_id=to_id))
    from . import notify
    giver = db.get(User, by) if by else None
    role = HANDOVER_KIND.get(kind, "")
    notify.send(db, [to_id], "handover", "Вам передан наряд" + (f" — вы {role}" if role else ""),
                f"{notify.car(db, wo)} · наряд {wo.number}" + (f" · передал {giver.short_name}" if giver else ""),
                f"/orders/{wo.id}", wo, skip=by)
    from .orders import event
    to = db.get(User, to_id)
    frm = db.get(User, from_id) if from_id else None
    event(db, wo, "status", f"Передан{'о' if kind == 'master' else ''}: {role} "
                            + (f"{frm.full_name} → " if frm else "") + (to.full_name if to else ""))


def pending(db: Session, wo: WorkOrder, uid) -> list[OrderHandover]:
    return list(db.scalars(select(OrderHandover).where(OrderHandover.work_order_id == wo.id, OrderHandover.to_id == uid,
                                                       OrderHandover.accepted_at.is_(None))))


def accept(db: Session, wo: WorkOrder, uid) -> int:
    n = 0
    for h in pending(db, wo, uid):
        h.accepted_at = now()
        n += 1
    if wo.manager_id == uid:
        wo.handover_pending = False
    return n


def pending_orders(db: Session, branch_id, uid) -> list:
    """Наряды, которые мне передали и я ещё не нажал «Принял» (только открытые)."""
    from ..models.orders import WO_ACTIVE
    return list(db.scalars(select(WorkOrder.id).join(OrderHandover, OrderHandover.work_order_id == WorkOrder.id).where(
        WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE), OrderHandover.to_id == uid,
        OrderHandover.accepted_at.is_(None)).distinct()))


def _cut():
    return datetime.now(timezone.utc) - timedelta(days=RO_DAYS)


def handed_out(db: Session, branch_id, uid) -> list:
    """Наряды, которые я (менеджер) передал другому менеджеру за последние 3 дня и которые сейчас не мои."""
    rows = db.scalars(select(WorkOrder.id).join(OrderHandover, OrderHandover.work_order_id == WorkOrder.id).where(
        WorkOrder.branch_id == branch_id, OrderHandover.kind == "manager", OrderHandover.from_id == uid,
        OrderHandover.created_at >= _cut(), WorkOrder.manager_id != uid).distinct())
    return list(rows)


def read_only(ctx, wo: WorkOrder) -> bool:
    """Прежний менеджер смотрит переданный наряд — только просмотр."""
    from .orders import manager_limited
    if ctx.user.id == wo.manager_id or not manager_limited(ctx):
        return False
    return wo.id in set(handed_out(ctx.db, ctx.branch.id, ctx.user.id))
