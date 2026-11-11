"""Деньги наряда: цена работ, итог, скидки, оплаты, возврат, предоплата, закрытие с актом выдачи.

С 11.11.2026 (решение брата): СТО НЕ работает в долг — машину выдаём только после полной оплаты, исключений нет.
Скидки две: на работы (works_discount_pct) и на весь наряд (discount_pct). Сначала скидка на работы, потом на весь наряд.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Payment, WoPart, WorkOrder, WoWork
from . import orders as osvc
from ..models.orders import PART_OFF

D0 = Decimal("0")
# Наряд закрыт для правок цен: после оплаты и выдачи цены и состав не меняются (отчёт тестировщика 11.11.2026)
FROZEN = ("paid", "closed", "cancel")


def money(x) -> Decimal:
    return Decimal(x or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def work_price(w: WoWork) -> Decimal:
    """По нормо-часам: часы × цена часа × кол-во. Фикс: цена × кол-во (цена за единицу лежит в rate)."""
    qty = Decimal(w.qty or 1)
    if w.pricing == "fixed":
        return money(Decimal(w.rate or 0) * qty)
    return money(Decimal(w.hours or 0) * Decimal(w.rate or 0) * qty)


def part_sum(p: WoPart) -> Decimal:
    """Сумма строки запчасти — округляем каждую строку, как на печати (иначе копейка расхождения)."""
    return money(Decimal(p.price or 0) * Decimal(p.qty or 0))


@dataclass
class Totals:
    works: Decimal
    parts: Decimal
    subtotal: Decimal
    discount: Decimal          # обе скидки вместе
    total: Decimal
    paid: Decimal
    works_discount: Decimal = D0
    order_discount: Decimal = D0

    @property
    def due(self) -> Decimal:
        return money(self.total - self.paid)


def pct(v) -> Decimal:
    return max(D0, min(Decimal(100), Decimal(v or 0)))


def totals(db: Session, wo: WorkOrder) -> Totals:
    works = money(sum((Decimal(w.price or 0) for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id))), D0))
    parts = money(sum((part_sum(p) for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id,
                                                                           WoPart.status.notin_(PART_OFF)))), D0))
    subtotal = money(works + parts)
    wd = money(works * pct(wo.works_discount_pct) / 100)
    od = money((subtotal - wd) * pct(wo.discount_pct) / 100)
    paid = money(db.scalar(select(func.sum(Payment.amount)).where(Payment.work_order_id == wo.id)) or 0)
    return Totals(works, parts, subtotal, money(wd + od), money(subtotal - wd - od), paid, wd, od)


def payroll_factor(wo: WorkOrder, works: bool) -> Decimal:
    """Во сколько раз скидки уменьшают базу зарплаты. Работы — обе скидки, запчасти — только скидка на наряд."""
    if wo.payroll_ignore_discount:
        return Decimal(1)
    f = (Decimal(100) - pct(wo.discount_pct)) / Decimal(100)
    if works:
        f = f * (Decimal(100) - pct(wo.works_discount_pct)) / Decimal(100)
    return f


def refresh_prepay(db: Session, wo: WorkOrder):
    """Предоплата внесена → снимаем блокировку подбора/закупки."""
    if wo.payment_type == "prepay" and wo.prepay_amount:
        wo.awaiting_prepay = totals(db, wo).paid < money(wo.prepay_amount)
    else:
        wo.awaiting_prepay = False


def check_payment(db: Session, wo: WorkOrder, amount: Decimal, purpose: str) -> str:
    """Можно ли принять оплату. Переплату не принимаем: сдачу кассир отдаёт сам."""
    t = totals(db, wo)
    old_debt = wo.status == "closed" and t.due > 0   # старый долг (выдан с долгом до 11.11.2026) — погасить можно
    if wo.status in FROZEN and not old_debt:
        return "Наряд оплачен или закрыт — оплату сюда принять нельзя"
    if amount <= 0:
        return "Сумма должна быть больше нуля"
    if purpose == "prepay":
        # Предоплата бывает раньше цен: не больше договорённой суммы (или остатка, если он больше)
        agreed = money(wo.prepay_amount) - t.paid if wo.payment_type == "prepay" and wo.prepay_amount else D0
        limit = max(agreed, t.due)
        if limit <= 0:
            return "Сначала впишите сумму предоплаты (блок «Предоплата»)"
        if amount > limit:
            return f"Больше, чем нужно: можно принять не больше {limit} ₴"
        return ""
    if t.due <= 0:
        return "Остатка к оплате нет"
    if amount > t.due:
        return f"Больше остатка: к оплате {t.due} ₴. Сдачу отдайте клиенту, а в CRM проведите {t.due} ₴"
    return ""


def add_payment(db: Session, wo: WorkOrder, amount, method: str, purpose: str, receipt_no: str = "", comment: str = "",
                form_token: str = "") -> Payment:
    p = Payment(work_order_id=wo.id, amount=money(amount), method=method, purpose=purpose,
                receipt_no=receipt_no.strip()[:60], comment=comment.strip()[:300], form_token=(form_token or None))
    db.add(p)
    db.flush()
    from ..models.money import PAY_METHODS, PAY_PURPOSES
    osvc.event(db, wo, "payment", f"{PAY_PURPOSES.get(purpose, purpose)}: {money(amount)} ₴ "
                                  f"({PAY_METHODS.get(method, method)}){', чек ' + receipt_no if receipt_no else ''}")
    refresh_prepay(db, wo)
    mark_paid_if_settled(db, wo)
    return p


def refund_limit(db: Session, wo: WorkOrder) -> Decimal:
    """Сколько можно вернуть клиенту. Оплаченный / выданный наряд — только переплату (сначала открыть заново)."""
    t = totals(db, wo)
    if wo.status in ("paid", "closed"):
        return max(D0, money(t.paid - t.total))
    return max(D0, t.paid)


def refund(db: Session, wo: WorkOrder, amount, method: str, reason: str) -> str:
    """Возврат денег клиенту (оформляет менеджер). В кассе — минус. Ошибка или ''."""
    a = money(amount)
    if not reason.strip():
        return "Укажите причину возврата"
    if a <= 0:
        return "Сумма возврата должна быть больше нуля"
    lim = refund_limit(db, wo)
    if a > lim:
        return f"Вернуть можно не больше {lim} ₴" if lim > 0 else "Возвращать нечего"
    db.add(Payment(work_order_id=wo.id, amount=-a, method=method, purpose="refund", comment=reason.strip()[:300]))
    db.flush()
    from ..models.money import PAY_METHODS
    osvc.event(db, wo, "payment", f"Возврат клиенту: {a} ₴ ({PAY_METHODS.get(method, method)}). {reason.strip()[:200]}")
    refresh_prepay(db, wo)
    return ""


def mark_paid_if_settled(db: Session, wo: WorkOrder):
    """Работа готова и долга нет → «Оплачено», и начисляется зарплата."""
    if wo.status == "ready" and totals(db, wo).due <= 0:
        osvc.set_status(db, wo, "paid", "Оплачено полностью")
        wo.paid_at = now()
        from . import payroll
        payroll.accrue_for_order(db, wo)


def guard_total(db: Session, wo: WorkOrder) -> str:
    """После правки цен: итог не может стать меньше уже оплаченного (тогда сначала возврат)."""
    t = totals(db, wo)
    if t.total < t.paid:
        return f"Итог {t.total} ₴ станет меньше оплаченного {t.paid} ₴ — сначала оформите возврат клиенту"
    return ""


def after_change(db: Session, wo: WorkOrder):
    """После любых правок работ/цен/скидки: наряд не должен «зависнуть».
    Все работы сделаны → «Готово»; оплачено полностью → «Оплачено»."""
    if wo.status == "work":
        works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)).all()
        if works and all(w.status == "done" for w in works):
            osvc.set_status(db, wo, "ready", "Все работы выполнены")
    elif wo.status == "ready":
        works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)).all()
        if any(w.status != "done" for w in works):
            osvc.set_status(db, wo, "work", "Добавлена работа — машина снова в ремонте")
            return
    mark_paid_if_settled(db, wo)


def not_issued_parts(db: Session, wo: WorkOrder) -> list[WoPart]:
    """Запчасти, которые есть в счёте, но слесарю не выданы — с ними машину не выдаём (склад не сойдётся)."""
    return db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id,
                                           WoPart.status.in_(("need", "ordered", "arrived", "reserved")))).all()


def close(db: Session, wo: WorkOrder, state: str, defects: str, advice: str) -> str:
    """Выдача авто клиенту — только после полной оплаты. Возвращает ошибку или ''."""
    if wo.status == "ready":   # например, открыли заново и всё уже оплачено
        mark_paid_if_settled(db, wo)
    t = totals(db, wo)
    if wo.status != "paid" or t.due > 0:
        return f"Сначала примите оплату: остаток {t.due} ₴. В долг машину не выдаём" if t.due > 0 \
            else "Выдать можно только оплаченный наряд"
    left = not_issued_parts(db, wo)
    if left:
        return (f"Запчасти не выданы со склада: {', '.join(p.name for p in left[:3])}"
                f"{' …' if len(left) > 3 else ''} — выдайте их слесарю или уберите из наряда")
    if state not in ("ok", "partial", "advice"):
        return "Выберите в акте выдачи: исправен, не полностью исправен или есть рекомендации"
    if state == "partial" and not defects.strip():
        return "Укажите, что именно неисправно"
    if state == "advice" and not advice.strip():
        return "Впишите рекомендации"
    wo.handover_state, wo.handover_defects, wo.handover_advice = state, defects.strip(), advice.strip()
    label = {"ok": "Автомобиль исправен", "partial": "Не полностью исправен", "advice": "Исправен, есть рекомендации"}[state]
    osvc.event(db, wo, "act", f"Акт выдачи: {label}")
    for line in [l.strip(" -•\t") for l in defects.splitlines() if l.strip(" -•\t")]:
        osvc.event(db, wo, "recommendation", f"Неисправно при выдаче: {line}", urgency="urgent")
    for line in [l.strip(" -•\t") for l in advice.splitlines() if l.strip(" -•\t")]:
        osvc.event(db, wo, "recommendation", line, urgency="advice")
    osvc.set_status(db, wo, "closed", label)
    wo.closed_at = now()
    return ""
