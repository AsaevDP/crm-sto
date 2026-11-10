"""Сообщения сотрудникам (11.11.2026). Клиенту ничего не отправляем — решение владельца.

Когда пишем:
- наряд перешёл в «Запчасти на складе» (пришло ВСЁ, от всех поставщиков) → менеджеру, слесарю, запчастисту:
  «Все запчасти на складе — можно начинать»; у слесаря в окошке кнопка «Запросить выдачу»;
- пришла деталь с пометкой «срочно», а остальное ещё едет → слесарю и менеджеру;
- слесарь пометил деталь «срочно» → запчастисту и менеджеру (кладовщик видит пометку в «Приходе» и «Выдаче»).
По каждой обычной детали не пишем — чтобы не отвлекать слесаря.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Notification, User, Vehicle, WorkOrder


def car(db: Session, wo: WorkOrder) -> str:
    from .orders import fmt_plate
    v = db.get(Vehicle, wo.vehicle_id)
    return f"{v.make} {v.model} · {fmt_plate(v.plate)}" if v else wo.number


def send(db: Session, user_ids, kind: str, title: str, text: str = "", url: str = "", wo: WorkOrder | None = None,
         skip=None):
    """Одно сообщение каждому из user_ids (без повторов, без пустых, без того, кто сам это сделал)."""
    seen = set()
    for uid in user_ids:
        if not uid or uid in seen or uid == skip:
            continue
        seen.add(uid)
        u = db.get(User, uid)
        if not u or not u.active:
            continue
        db.add(Notification(user_id=uid, kind=kind, title=title[:200], text=text[:400], url=url[:200],
                            work_order_id=wo.id if wo else None))


def team(wo: WorkOrder) -> list:
    """Кто задействован в наряде: менеджер, слесарь, запчастист."""
    return [wo.manager_id, wo.mechanic_id, wo.partsman_id]


def parts_ready(db: Session, wo: WorkOrder):
    # «Срочная деталь пришла» по этому наряду больше не нужна — теперь пришло всё
    for n in db.scalars(select(Notification).where(Notification.work_order_id == wo.id, Notification.kind == "urgent_in",
                                                   Notification.read_at.is_(None))):
        n.read_at = now()
    send(db, team(wo), "parts_ready", "Все запчасти на складе — можно начинать",
         f"{car(db, wo)} · наряд {wo.number}", f"/orders/{wo.id}#parts", wo)


def unread(db: Session, user_id, limit: int = 10) -> list[Notification]:
    return list(db.scalars(select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
                           .order_by(Notification.created_at.desc()).limit(limit)))


def recent(db: Session, user_id, limit: int = 50) -> list[Notification]:
    return list(db.scalars(select(Notification).where(Notification.user_id == user_id)
                           .order_by(Notification.created_at.desc()).limit(limit)))


def mark_read(db: Session, user_id, nid=None):
    q = select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if nid:
        q = q.where(Notification.id == nid)
    for n in db.scalars(q):
        n.read_at = now()
