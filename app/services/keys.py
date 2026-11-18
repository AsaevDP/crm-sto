"""Ячейки ключей (18.11.2026, брат).

У каждого сотрудника на рабочем месте (подъёмник, бокс мойки, менеджер…) — шкаф с крючками 1…N (по умолчанию 50,
«Настройки → Ячейки ключей»). Каждый наряд при приёмке сам получает номер крючка — WorkOrder.key_slot; все, кто
работает с машиной, вешают ключи на этот номер в своём шкафу. Номер не меняется до выдачи машины и вручную не
меняется никем (решение владельца: чтобы не было путаницы).

• Крючок занят, пока наряд открыт (WO_ACTIVE). «Выдать авто» или отмена — крючок свободен, его может занять другая машина.
• Номер — следующий свободный ПО КРУГУ после последнего выданного (31, 32, 33…): только что освободившийся крючок
  не занимают сразу — ключи прошлой машины успеют снять.
• Все крючки заняты — машину принимаем, key_slot = 0: в наряде пометка «Без ячейки» (видят все по машине).
• key_slot = None — наряд из времени до ячеек; открытым таким нарядам номер раздаётся при запуске (fill_open).
• Наряд открыли заново (гарантия, «Открыть заново») — машина снова на станции: новый номер (старый мог занять другой).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WorkOrder
from ..models.orders import WO_ACTIVE
from . import settings as st

NONE = 0           # «Без ячейки»: все крючки были заняты
DEFAULT_COUNT = 50


def count(db: Session, branch_id) -> int:
    try:
        return max(int(st.get(db, branch_id, "keys").get("count") or DEFAULT_COUNT), 1)
    except (TypeError, ValueError):
        return DEFAULT_COUNT


def busy(db: Session, branch_id, exclude_id=None) -> set[int]:
    q = select(WorkOrder.key_slot).where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE),
                                        WorkOrder.key_slot.is_not(None), WorkOrder.key_slot > 0)
    if exclude_id is not None:
        q = q.where(WorkOrder.id != exclude_id)
    return set(db.scalars(q))


def assign(db: Session, wo: WorkOrder) -> int:
    """Выдать наряду номер крючка (следующий свободный по кругу). Нет свободных — 0 («Без ячейки»)."""
    bid = wo.branch_id
    n = count(db, bid)
    taken = busy(db, bid, exclude_id=wo.id)
    last = int(st.get(db, bid, "keys").get("last") or 0)
    slot = NONE
    for i in range(1, n + 1):
        cand = (last + i - 1) % n + 1
        if cand not in taken:
            slot = cand
            break
    wo.key_slot = slot
    if slot:
        st.put(db, bid, "keys", {"last": slot})
    return slot


def label(wo: WorkOrder) -> str:
    if wo.key_slot is None:
        return ""
    return f"{wo.key_slot}" if wo.key_slot > 0 else "Без ячейки"


def fill_open(db: Session) -> int:
    """При запуске: открытым нарядам без номера (заведены до ячеек, демо) — номер по порядку приезда."""
    rows = db.scalars(select(WorkOrder).where(WorkOrder.status.in_(WO_ACTIVE), WorkOrder.key_slot.is_(None))
                      .order_by(WorkOrder.created_at)).all()
    from ..db import current_branch_id
    for wo in rows:
        current_branch_id.set(wo.branch_id)
        assign(db, wo)
        db.flush()
    return len(rows)


def stats(db: Session, branch_id) -> dict:
    n = count(db, branch_id)
    taken = busy(db, branch_id)
    none = db.scalar(select(WorkOrder.id).where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE),
                                                WorkOrder.key_slot == NONE).limit(1))
    return {"count": n, "busy": len(taken), "free": n - len([s for s in taken if s <= n]), "without": bool(none),
            "max_busy": max(taken) if taken else 0}
