"""Деньги наряда: цена работ, итог, скидка, оплаты, предоплата, закрытие с актом выдачи."""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Payment, WoPart, WorkOrder, WoWork
from . import orders as osvc

D0 = Decimal("0")


def money(x) -> Decimal:
    return Decimal(x or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def work_price(w: WoWork) -> Decimal:
    """По нормо-часам: часы × цена часа × кол-во. Фикс: цена × кол-во (цена за единицу лежит в rate)."""
    qty = Decimal(w.qty or 1)
    if w.pricing == "fixed":
        return money(Decimal(w.rate or 0) * qty)
    return money(Decimal(w.hours or 0) * Decimal(w.rate or 0) * qty)


@dataclass
class Totals:
    works: Decimal
    parts: Decimal
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    paid: Decimal

    @property
    def due(self) -> Decimal:
        return money(self.total - self.paid)


def totals(db: Session, wo: WorkOrder) -> Totals:
    works = sum((w.price for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id))), D0)
    parts = sum((p.price * p.qty for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id,
                                                                            WoPart.status != "returned"))), D0)
    subtotal = money(works + parts)
    discount = money(subtotal * Decimal(wo.discount_pct or 0) / 100)
    paid = money(db.scalar(select(func.sum(Payment.amount)).where(Payment.work_order_id == wo.id)) or 0)
    return Totals(money(works), money(parts), subtotal, discount, money(subtotal - discount), paid)


def refresh_prepay(db: Session, wo: WorkOrder):
    """Предоплата внесена → снимаем блокировку подбора/закупки."""
    if wo.payment_type == "prepay" and wo.prepay_amount:
        wo.awaiting_prepay = totals(db, wo).paid < money(wo.prepay_amount)
    else:
        wo.awaiting_prepay = False


def add_payment(db: Session, wo: WorkOrder, amount, method: str, purpose: str, receipt_no: str = "", comment: str = "") -> Payment:
    p = Payment(work_order_id=wo.id, amount=money(amount), method=method, purpose=purpose,
                receipt_no=receipt_no.strip()[:60], comment=comment.strip()[:300])
    db.add(p)
    db.flush()
    from ..models.money import PAY_METHODS, PAY_PURPOSES
    osvc.event(db, wo, "payment", f"{PAY_PURPOSES.get(purpose, purpose)}: {money(amount)} ₴ "
                                  f"({PAY_METHODS.get(method, method)}){', чек ' + receipt_no if receipt_no else ''}")
    refresh_prepay(db, wo)
    mark_paid_if_settled(db, wo)
    return p


def mark_paid_if_settled(db: Session, wo: WorkOrder):
    """Работа готова и долга нет → «Оплачено», и начисляется зарплата."""
    if wo.status == "ready" and totals(db, wo).due <= 0:
        osvc.set_status(db, wo, "paid", "Оплачено полностью")
        wo.paid_at = now()
        from . import payroll
        payroll.accrue_for_order(db, wo)


def close(db: Session, wo: WorkOrder, is_admin: bool, state: str, defects: str, advice: str, debt_comment: str = "") -> str:
    """Выдача авто клиенту. Возвращает ошибку или ''."""
    t = totals(db, wo)
    if wo.status not in ("paid", "ready"):
        return "Выдать можно только готовый наряд"
    if t.due > 0 or wo.status == "ready":
        if not is_admin:
            return f"Остаток к оплате {t.due} ₴ — закрыть с долгом может только главный админ"
        if not debt_comment.strip():
            return "Закрытие с долгом: укажите комментарий"
    if state not in ("ok", "partial", "advice"):
        return "Выберите в акте выдачи: исправен, не полностью исправен или есть рекомендации"
    if state == "partial" and not defects.strip():
        return "Укажите, что именно неисправно"
    if state == "advice" and not advice.strip():
        return "Впишите рекомендации"
    wo.handover_state, wo.handover_defects, wo.handover_advice = state, defects.strip(), advice.strip()
    if t.due > 0 or wo.status == "ready":
        wo.closed_with_debt_comment = debt_comment.strip()
        if wo.status == "ready":   # закрываем без полной оплаты — зарплату всё равно начисляем
            from . import payroll
            payroll.accrue_for_order(db, wo)
    label = {"ok": "Автомобиль исправен", "partial": "Не полностью исправен", "advice": "Исправен, есть рекомендации"}[state]
    osvc.event(db, wo, "act", f"Акт выдачи: {label}" + (f". Долг {t.due} ₴: {debt_comment.strip()}" if t.due > 0 else ""))
    for line in [l.strip(" -•\t") for l in defects.splitlines() if l.strip(" -•\t")]:
        osvc.event(db, wo, "recommendation", f"Неисправно при выдаче: {line}", urgency="urgent")
    for line in [l.strip(" -•\t") for l in advice.splitlines() if l.strip(" -•\t")]:
        osvc.event(db, wo, "recommendation", line, urgency="advice")
    osvc.set_status(db, wo, "closed", label)
    wo.closed_at = now()
    return ""
