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


def masters(db: Session, wo: WorkOrder) -> list:
    """Все мастера наряда: ведущий и каждый, у кого в наряде своя работа (электрик, моторист, второй слесарь…)."""
    from ..models import WoWork
    ids = [wo.mechanic_id, *db.scalars(select(WoWork.mechanic_id).where(WoWork.work_order_id == wo.id,
                                                                         WoWork.mechanic_id.is_not(None)))]
    return list(dict.fromkeys(i for i in ids if i))


def team_all(db: Session, wo: WorkOrder) -> list:
    """Менеджер, запчастист и все мастера наряда (12.11.2026: в одном заказе бывают детали для разных мастеров)."""
    return [wo.manager_id, wo.partsman_id, *masters(db, wo)]


def parts_ready(db: Session, wo: WorkOrder):
    # «Пришли запчасти» / «Срочная деталь пришла» по этому наряду больше не нужны — теперь пришло всё
    for n in db.scalars(select(Notification).where(Notification.work_order_id == wo.id,
                                                   Notification.kind.in_(("urgent_in", "parts_in")),
                                                   Notification.read_at.is_(None))):
        n.read_at = now()
    send(db, team_all(db, wo), "parts_ready", "Все запчасти на складе — можно начинать",
         f"{car(db, wo)} · наряд {wo.number}", f"/orders/{wo.id}#parts", wo)


def parts_arrived(db: Session, wo: WorkOrder, parts: list):
    """Кладовщик принял часть запчастей наряда, остальное ещё едет (12.11.2026) — одно сообщение на приёмку
    менеджеру, запчастисту и всем мастерам наряда: сколько пришло и сколько всего уже на складе."""
    from ..models import WoPart
    from ..models.orders import PART_OFF
    if not parts:
        return
    allp = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.notin_(PART_OFF))).all()
    have = sum(1 for p in allp if p.status in ("reserved", "issued", "installed"))
    urgent = any(p.urgent for p in parts)
    names = ", ".join(("🔥 " if p.urgent else "") + p.name for p in parts[:4]) + ("…" if len(parts) > 4 else "")
    title = (f"Срочная деталь пришла: {parts[0].name}" if urgent and len(parts) == 1
             else f"Пришли запчасти: {len(parts)} шт." + (" (есть срочная)" if urgent else ""))
    send(db, team_all(db, wo), "urgent_in" if urgent else "parts_in", title,
         f"{names} · на складе {have} из {len(allp)} · {car(db, wo)} · наряд {wo.number}", f"/orders/{wo.id}#parts", wo)


def arrived_waiting(db: Session, branch_id, user_id) -> dict:
    """Зелёная цифра «пришло» (12.11.2026): по моим открытым нарядам — сколько запчастей пришло по закупке и лежит
    в резерве (ещё не выдано мастеру). Гаснет, когда кладовщик выдал. {наряд: штук}."""
    from sqlalchemy import func, or_
    from ..models import PurchaseLine, WoPart, WoWork
    q = (select(WoPart.work_order_id, func.count(func.distinct(WoPart.id)))
         .join(WorkOrder, WorkOrder.id == WoPart.work_order_id)
         .join(PurchaseLine, PurchaseLine.wo_part_id == WoPart.id)
         .where(WorkOrder.branch_id == branch_id, WoPart.status == "reserved",
                WorkOrder.status.not_in(("closed", "cancel")),
                or_(PurchaseLine.received.is_(True), PurchaseLine.received_qty > 0),
                or_(WorkOrder.manager_id == user_id, WorkOrder.partsman_id == user_id, WorkOrder.mechanic_id == user_id,
                    WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == user_id))))
         .group_by(WoPart.work_order_id))
    return {wid: n for wid, n in db.execute(q)}


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
