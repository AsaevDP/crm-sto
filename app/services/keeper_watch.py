"""Кладовщику по нарядам (23.11.2026, брат) — что держать в голове до запроса на выдачу.

🟣 «Отложено под наряд» (для сведения): запчастист во время подбора взял деталь со склада в резерв. Горит, пока мастер
   не подтвердил «✓ Получил» (резерв → выдача → получено).
🟠 «Подготовить к выдаче»: менеджер «Согласовал с клиентом» — деталь лежит в резерве под наряд, а запроса на выдачу по
   ней ещё нет. Гаснет, когда мастер запросил выдачу (дальше обычная 🔴 «просят») или деталь выдали.

Зоны: кладовщик склада видит новые детали, кладовщик разбора — б/у, главный кладовщик — обе.
"""
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import IssueRequest, IssueRequestLine, PartCatalog, PurchaseLine, WoPart, WorkOrder

AFTER_APPROVE = ("picking", "waiting", "instock", "work", "ready", "paid")


def zones_for(db: Session, user) -> tuple:
    from ..permissions import effective_permissions
    perms = effective_permissions(db, user)
    return tuple(z for z, p in (("new", "stock.issue"), ("razbor", "razbor.issue")) if p in perms)


def _zone_cond(zones):
    used = select(PartCatalog.id).where(PartCatalog.kind == "used")
    if set(zones) >= {"new", "razbor"}:
        return None
    if "razbor" in zones:
        return WoPart.catalog_id.in_(used)
    if "new" in zones:
        return or_(WoPart.catalog_id.is_(None), WoPart.catalog_id.not_in(used))
    return WoPart.id.is_(None)   # ничьей зоны — ничего


def _group(rows) -> dict:
    out: dict = {}
    for wid, pid in rows:
        out.setdefault(wid, []).append(pid)
    return out


def prepare(db: Session, branch_id, zones) -> dict:
    """🟠 {наряд: [детали]} — согласовано, лежит в резерве, запроса на выдачу ещё не было."""
    if not zones:
        return {}
    asked = select(IssueRequestLine.wo_part_id).join(IssueRequest, IssueRequest.id == IssueRequestLine.request_id).where(
        IssueRequest.status != "cancelled")
    q = (select(WoPart.work_order_id, WoPart.id).join(WorkOrder, WorkOrder.id == WoPart.work_order_id)
         .where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(AFTER_APPROVE), WoPart.status == "reserved",
                WoPart.id.not_in(asked)))
    zc = _zone_cond(zones)
    if zc is not None:
        q = q.where(zc)
    return _group(db.execute(q))


def held(db: Session, branch_id, zones) -> dict:
    """🟣 {наряд: [детали]} — взято со склада под наряд (не по закупке), пока мастер не нажал «Получил»."""
    if not zones:
        return {}
    bought = select(PurchaseLine.wo_part_id).where(PurchaseLine.wo_part_id.is_not(None))
    unreceived = select(IssueRequestLine.wo_part_id).join(IssueRequest, IssueRequest.id == IssueRequestLine.request_id).where(
        IssueRequest.status == "issued", IssueRequest.received_at.is_(None))
    q = (select(WoPart.work_order_id, WoPart.id).join(WorkOrder, WorkOrder.id == WoPart.work_order_id)
         .where(WorkOrder.branch_id == branch_id, WorkOrder.status.not_in(("closed", "cancel")),
                WoPart.id.not_in(bought),
                or_(WoPart.status == "reserved",
                    (WoPart.status.in_(("issued", "installed"))) & WoPart.id.in_(unreceived))))
    zc = _zone_cond(zones)
    if zc is not None:
        q = q.where(zc)
    return _group(db.execute(q))


def notify_prepare(db: Session, wo: WorkOrder):
    """«Согласовал с клиентом» — кладовщику окошко «Подготовьте к выдаче» (по своей зоне)."""
    from . import messages, notify
    from . import stock as ssvc
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "reserved")).all()
    by_zone: dict = {}
    for p in parts:
        by_zone.setdefault(ssvc.part_zone(db, p), []).append(p)
    for zone, items in by_zone.items():
        names = ", ".join(p.name for p in items[:6]) + ("…" if len(items) > 6 else "")
        notify.send(db, messages.storekeepers(db, wo.branch_id, zone), "prepare",
                    f"Согласовано — подготовьте к выдаче · {wo.number}",
                    f"{notify.car(db, wo)} · {names}", "/stock/reserved", wo)


def notify_held(db: Session, wo: WorkOrder):
    """«Подбор завершён» — кладовщику для сведения: что запчастист отложил со склада под наряд."""
    from . import messages, notify
    from . import stock as ssvc
    bought = set(db.scalars(select(PurchaseLine.wo_part_id).where(PurchaseLine.wo_part_id.is_not(None))))
    parts = [p for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "reserved"))
             if p.id not in bought]
    by_zone: dict = {}
    for p in parts:
        by_zone.setdefault(ssvc.part_zone(db, p), []).append(p)
    for zone, items in by_zone.items():
        names = ", ".join(p.name for p in items[:6]) + ("…" if len(items) > 6 else "")
        notify.send(db, messages.storekeepers(db, wo.branch_id, zone), "held_info",
                    f"Отложено со склада под наряд · {wo.number}", f"{notify.car(db, wo)} · {names}",
                    "/stock/reserved", wo)
