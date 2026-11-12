"""Ремонт по гарантии (12.11.2026, решение брата).

Клиент вернулся по гарантии → менеджер находит старый наряд (поиск по точным данным) → «Ремонт по гарантии» с описанием →
решает директор или зам (сам менеджер — нет: гарантию всегда разбирает руководитель) → наряд открыт как гарантийный:
работы по гарантии — 0 ₴, мастер делает их как обычно → «Выполнены гарантийные работы» (обязательно: что произошло
и почему) → наряд закрыт второй раз, отметки в наряде, истории клиента и в акте.
Не гарантийный случай → «Не гарантийный случай» с причиной: наряд закрывается как был, ремонт — новым нарядом через приёмку.
Зарплата по первому ремонту не трогается; гарантийные работы бесплатны — начислений по ним нет.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import WoWork, WorkOrder
from . import orders as osvc


def open_(db: Session, wo: WorkOrder, reason: str):
    """Директор / зам разрешил — наряд открыт по гарантии (машина снова у нас)."""
    wo.warranty, wo.warranty_reason, wo.warranty_opened_at = "open", reason.strip()[:2000], now()
    wo.warranty_result, wo.warranty_closed_at, wo.warranty_closed_by = "", None, None
    wo.warranty_prev_closed = wo.closed_at
    wo.closed_at = None
    osvc.set_status(db, wo, "ready", f"Открыт по гарантии: {reason.strip()[:200]}")
    osvc.event(db, wo, "status", f"Ремонт по гарантии: {reason.strip()[:300]}")


def done(db: Session, wo: WorkOrder, user, result: str) -> str:
    """«Выполнены гарантийные работы» — наряд закрыт второй раз. Ошибка или ''."""
    from . import money as msvc
    if wo.warranty != "open":
        return "Наряд не открыт по гарантии"
    if not result.strip():
        return "Напишите, что произошло и почему — это попадёт в наряд, историю клиента и акт"
    works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.is_warranty.is_(True))).all()
    if not works:
        return "Добавьте гарантийные работы (что сделали) — или нажмите «Не гарантийный случай»"
    if any(w.status != "done" for w in works):
        return "Не все гарантийные работы выполнены"
    if msvc.not_issued_parts(db, wo):
        return "Есть запчасти, не выданные со склада — выдайте или уберите их"
    t = msvc.totals(db, wo)
    if t.due > 0:
        return f"Есть к оплате {msvc.money(t.due)} ₴ — гарантия бесплатна; если клиент платит за доп. работы, примите оплату"
    _close(db, wo, user, result, "done")
    osvc.event(db, wo, "act", f"Гарантийный ремонт выполнен: {result.strip()[:300]}")
    return ""


def reject(db: Session, wo: WorkOrder, user, result: str) -> str:
    """«Не гарантийный случай» — наряд закрывается как был; ремонт — новым нарядом через приёмку."""
    if wo.warranty != "open":
        return "Наряд не открыт по гарантии"
    if not result.strip():
        return "Напишите, почему случай не гарантийный"
    works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.is_warranty.is_(True))).all()
    if any(w.status == "done" for w in works):
        return "Гарантийные работы уже выполнены — закройте кнопкой «Выполнены гарантийные работы»"
    for w in works:
        db.delete(w)
    db.flush()
    _close(db, wo, user, result, "rejected")
    osvc.event(db, wo, "note", f"Не гарантийный случай: {result.strip()[:300]}. Ремонт — новым нарядом")
    return ""


def _close(db: Session, wo: WorkOrder, user, result: str, how: str):
    wo.warranty, wo.warranty_result = how, result.strip()[:2000]
    wo.warranty_closed_at, wo.warranty_closed_by = now(), user.id
    wo.closed_at = wo.warranty_prev_closed or now()    # в отчётах ремонт остаётся в дне первой выдачи
    label = "Выполнены гарантийные работы" if how == "done" else "Не гарантийный случай"
    osvc.set_status(db, wo, "closed", f"{label}: {result.strip()[:200]}")
    from .decisions import deciders
    from . import notify as nsvc
    nsvc.send(db, [u.id for u in deciders(db, wo.branch_id)] + [wo.manager_id], "warranty_done",
              f"Гарантия по наряду {wo.number}: {label.lower()}", result.strip()[:300], f"/orders/{wo.id}", wo, skip=user.id)
