"""Уведомления на странице «Сообщения» (20.11.2026, брат): группы для плиток-фильтров и «✓ сделано».

Уведомление о деле (подбор готов, пришли запчасти, просят химию, вам передали наряд…) после того, как дело сделано,
помечается «✓ сделано» и в красную цифру «Сообщений» больше не идёт — даже если его не открывали.
Сообщения «для сведения» (ответ менеджера, переписка, отказ) — просто прочитано / не прочитано.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import IssueRequest, Notification, OrderMessage, WoPart, WorkOrder

# Плитки-фильтры: ключ → (подпись, виды уведомлений)
GROUPS = {
    "parts": ("Запчасти", {"parts_in", "parts_ready", "picked", "to_order", "new_problem", "diag_to_mgr", "order_cancel", "refused", "refused_done",
                           "refused_info", "urgent_in", "urgent_ask"}),
    "stock": ("Склад и выдача", {"issue_ready", "issued", "part_go", "part_ask", "part_ask_info", "part_ok", "part_no",
                                 "tool_ready", "tool_issued", "chem_ask", "chem_ready", "chem_ok", "chem_no"}),
    "msg": ("Переписка", {"msg"}),
    "people": ("Передачи и доступы", {"handover", "access_req", "access_ok", "access_no", "client_req", "client_ok",
                                      "client_no", "reopen_req", "reopen_no", "self_decided", "payroll_req", "payroll_ok",
                                      "payroll_no", "wash_queue", "wash_queue_ok", "wash_queue_no", "to_wash", "wash_go"}),
}
CLOSED = ("closed", "cancel", "cancelled")


def group_of(kind: str) -> str:
    return next((k for k, (_, kinds) in GROUPS.items() if kind in kinds), "other")


def _count(db, q) -> int:
    return db.scalar(select(func.count()).select_from(q.subquery())) or 0


def is_done(db: Session, n: Notification, user) -> bool:
    """Дело, о котором уведомление, уже сделано (или наряд закрыт). Сообщения «для сведения» — False."""
    k, wid, bid = n.kind, n.work_order_id, n.branch_id
    wo = db.get(WorkOrder, wid) if wid else None
    if wo is not None and wo.status in CLOSED and k not in ("reopen_req",):
        return True
    if k in ("parts_in", "parts_ready", "urgent_in"):   # пришло — пока всё не выдано мастеру
        return wo is not None and not _count(db, select(WoPart.id).where(WoPart.work_order_id == wid,
                                                                         WoPart.status == "reserved"))
    if k == "picked":   # подбор готов — менеджеру согласовать
        return wo is not None and wo.status != "approve"
    if k == "diag_to_mgr":   # 23.11.2026: мастер закончил — менеджеру вписать проблемы и «На согласование»
        return wo is not None and wo.status != "diag"
    if k == "new_problem":   # 23.11.2026: новая проблема — пока наряд на согласовании (запчастисту — пока не подобрал)
        return wo is not None and (wo.status != "approve" or (user.id == wo.partsman_id and wo.picked_at is not None))
    if k == "to_order":   # запчастисту: заказать (черновик закупки по наряду)
        from ..models import Purchase
        return wo is not None and not _count(db, select(Purchase.id).where(Purchase.work_order_id == wid,
                                                                           Purchase.status == "draft"))
    if k == "refused":   # отказ от заказанного — запчастист решает
        from ..models import PartLeftover
        return not _count(db, select(PartLeftover.id).where(PartLeftover.work_order_id == wid,
                                                            PartLeftover.status.in_(("decide", "stock"))))
    if k == "issue_ready":   # кладовщик подготовил — забрать
        return not _count(db, select(IssueRequest.id).where(IssueRequest.work_order_id == wid,
                                                            IssueRequest.status == "ready"))
    if k == "part_go":   # кладовщику: выдать
        return not _count(db, select(IssueRequest.id).where(IssueRequest.work_order_id == wid,
                                                            IssueRequest.status.in_(("new", "ready"))))
    if k in ("part_ask", "part_ask_info"):   # мастер просит со склада — пока менеджер не решил
        return not _count(db, select(OrderMessage.id).where(OrderMessage.work_order_id == wid, OrderMessage.kind == "part",
                                                            OrderMessage.status == "pending"))
    if k == "issued":   # выдано — мастеру подтвердить «Получил»
        from . import receipt
        return not any(w.id == wid for _, w in receipt.for_master(db, bid, user.id))
    if k == "tool_ready":
        from ..models import ToolRequest
        return not _count(db, select(ToolRequest.id).where(ToolRequest.user_id == user.id, ToolRequest.status == "ready"))
    if k == "tool_issued":
        from . import receipt
        return not receipt.tools_for_master(db, bid, user.id)
    if k == "chem_ask":   # кладовщику: просят химию
        from ..models import ChemRequest
        return not _count(db, select(ChemRequest.id).where(ChemRequest.branch_id == bid, ChemRequest.status == "new"))
    if k == "chem_ready":
        from ..models import ChemRequest
        return not _count(db, select(ChemRequest.id).where(ChemRequest.created_by == user.id, ChemRequest.status == "ready"))
    if k == "handover":
        from .handover import pending_orders
        return wid not in set(pending_orders(db, bid, user.id))
    if k == "access_req":
        from ..models import OrderAccess
        return not _count(db, select(OrderAccess.id).where(OrderAccess.branch_id == bid, OrderAccess.status == "pending"))
    if k == "client_req":
        from ..models import ClientAccess
        return not _count(db, select(ClientAccess.id).where(ClientAccess.branch_id == bid, ClientAccess.status == "pending"))
    if k == "reopen_req":
        from ..models import OrderReopen
        return not _count(db, select(OrderReopen.id).where(OrderReopen.work_order_id == wid, OrderReopen.status == "pending"))
    if k in ("to_wash", "wash_go"):   # 20.11.2026: машина с ремонта ждёт мойки — пока мойку не сделали
        from ..models import WoWork
        return wo is None or not _count(db, select(WoWork.id).where(WoWork.work_order_id == wid, WoWork.status != "done"))
    if k == "wash_queue":
        from ..models import WashQueueRequest
        return not _count(db, select(WashQueueRequest.id).where(WashQueueRequest.work_order_id == wid,
                                                                WashQueueRequest.status == "new"))
    if k == "payroll_req":
        from ..models.payroll import PayrollAccess
        return not _count(db, select(PayrollAccess.id).where(PayrollAccess.branch_id == bid, PayrollAccess.status == "pending"))
    return False


def unread_open(db: Session, user, limit: int = 10) -> list[Notification]:
    """Непрочитанные, дело по которым ещё не сделано (для красной цифры «Сообщений» и окошек): сделанные
    не считаются и окошком не всплывают."""
    out = []
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
                        .order_by(Notification.created_at.desc()).limit(200)):
        if is_done(db, n, user):
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out
