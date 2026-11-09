"""Зарплата: схемы (конструктор) и начисления.

Начисляется, когда наряд оплачен:
  слесарю     — % (или фикс за шт) от СВОИХ выполненных работ;
  запчастисту — % от НАЦЕНКИ (продажа − закупка) по запчастям, которые он подбирал (или фикс за шт).
Скидка клиента уменьшает базу у всех, если в наряде не включено «Зарплата без учёта скидки».
Если у сотрудника нет своей схемы — берутся проценты по умолчанию из настроек.
Из нескольких подходящих строк берётся самая точная: деталь → группа → откуда деталь → любая.
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (PartGroupItem, PayrollAccrual, PayrollPayout, PayScheme, PaySchemeLine, User, UserPayScheme, WoPart,
                      WorkOrder, WoWork)
from . import settings as st
from .money import money

KINDS = {"percent": "Процент", "per_unit": "Фикс за штуку", "salary": "Оклад"}
BASES = {"works": "от суммы работ", "parts_margin": "от наценки запчастей", "parts_sale": "от цены продажи запчастей"}
SOURCES = {"": "любые", "purchased": "закупил под наряд", "from_stock": "взял со склада"}
PERIODS = {"day": "в день", "month": "в месяц"}


# ---------- Схема сотрудника ----------
def user_scheme(db: Session, user: User, create: bool = False) -> PayScheme | None:
    """Личная схема сотрудника (одна действующая)."""
    link = db.scalar(select(UserPayScheme).where(UserPayScheme.user_id == user.id, UserPayScheme.valid_to.is_(None))
                     .order_by(UserPayScheme.valid_from.desc()))
    if link:
        return db.get(PayScheme, link.scheme_id)
    if not create:
        return None
    s = PayScheme(name=f"Схема: {user.full_name}")
    db.add(s)
    db.flush()
    db.add(UserPayScheme(user_id=user.id, scheme_id=s.id, valid_from=date.today()))
    db.flush()
    return s


def lines_for(db: Session, user: User) -> list[PaySchemeLine]:
    s = user_scheme(db, user)
    if not s:
        return []
    return db.scalars(select(PaySchemeLine).where(PaySchemeLine.scheme_id == s.id)
                      .order_by(PaySchemeLine.priority.desc(), PaySchemeLine.created_at)).all()


def default_lines(db: Session, user: User) -> list[PaySchemeLine]:
    """Нет своей схемы → проценты по умолчанию из настроек (объекты не сохраняются в БД)."""
    cfg = st.get(db, user.branch_id, "payroll")
    if user.role.code == "mechanic":
        return [PaySchemeLine(kind="percent", base="works", rate=Decimal(str(cfg["mechanic_works_pct"])), priority=0)]
    if user.role.code == "partsman":
        return [PaySchemeLine(kind="percent", base="parts_margin", rate=Decimal(str(cfg["partsman_margin_pct"])), priority=0)]
    return []


def _groups_of(db: Session, catalog_id) -> set:
    if not catalog_id:
        return set()
    return set(db.scalars(select(PartGroupItem.group_id).where(PartGroupItem.catalog_id == catalog_id)))


def _best(lines, bases, part: WoPart | None, groups: set):
    """Самая точная подходящая строка схемы."""
    best, best_score = None, -1
    for l in lines:
        if l.kind not in ("percent", "per_unit") or (l.kind == "percent" and l.base not in bases):
            continue
        if l.kind == "per_unit" and part is None and l.base != "works":
            continue
        score = 0
        if l.filter_catalog_id:
            if not part or l.filter_catalog_id != part.catalog_id:
                continue
            score += 8
        if l.filter_group_id:
            if l.filter_group_id not in groups:
                continue
            score += 4
        if l.filter_source:
            if not part or l.filter_source != part.source:
                continue
            score += 2
        score = score * 100 + (l.priority or 0)
        if score > best_score:
            best, best_score = l, score
    return best


# ---------- Начисление по наряду ----------
def accrue_for_order(db: Session, wo: WorkOrder):
    """Начислить зарплату по наряду. Повторно не начисляет (идемпотентно)."""
    factor = Decimal(1) if wo.payroll_ignore_discount else (Decimal(100) - Decimal(wo.discount_pct or 0)) / Decimal(100)
    today = date.today()
    done = set(db.scalars(select(PayrollAccrual.wo_work_id).where(PayrollAccrual.work_order_id == wo.id,
                                                                  PayrollAccrual.status != "cancelled")))
    done_p = set(db.scalars(select(PayrollAccrual.wo_part_id).where(PayrollAccrual.work_order_id == wo.id,
                                                                    PayrollAccrual.status != "cancelled")))
    users = {}

    def lines(uid):
        if uid not in users:
            u = db.get(User, uid)
            users[uid] = (u, (lines_for(db, u) or default_lines(db, u)) if u else [])
        return users[uid][1]

    # Слесарь: свои выполненные работы
    for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.status == "done")):
        uid = w.mechanic_id or wo.mechanic_id
        if not uid or w.id in done:
            continue
        line = _best(lines(uid), {"works"}, None, set())
        if not line:
            continue
        base = money(Decimal(w.price) * factor)
        amount = money(base * line.rate / 100) if line.kind == "percent" else money(line.rate * Decimal(w.qty or 1))
        db.add(PayrollAccrual(user_id=uid, work_order_id=wo.id, wo_work_id=w.id, scheme_line_id=line.id,
                              base_amount=base, rate=line.rate, amount=amount, on_date=today, kind=line.kind,
                              note=w.name[:300]))

    # Запчастист: подобранные им и выданные запчасти
    for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.in_(("issued", "installed")))):
        if not p.picked_by or p.id in done_p:
            continue
        line = _best(lines(p.picked_by), {"parts_margin", "parts_sale"}, p, _groups_of(db, p.catalog_id))
        if not line:
            continue
        qty = Decimal(p.qty or 1)
        if line.kind == "per_unit":
            base, amount = money(qty), money(line.rate * qty)
        else:
            raw = (Decimal(p.price) - Decimal(p.cost)) * qty if line.base == "parts_margin" else Decimal(p.price) * qty
            base = money(max(raw, Decimal(0)) * factor)
            amount = money(base * line.rate / 100)
        db.add(PayrollAccrual(user_id=p.picked_by, work_order_id=wo.id, wo_part_id=p.id, scheme_line_id=line.id,
                              base_amount=base, rate=line.rate, amount=amount, on_date=today, kind=line.kind,
                              note=p.name[:300]))


def cancel_for_order(db: Session, wo: WorkOrder):
    for a in db.scalars(select(PayrollAccrual).where(PayrollAccrual.work_order_id == wo.id,
                                                     PayrollAccrual.status == "accrued")):
        a.status = "cancelled"


def accrue_salary(db: Session, user: User, days: int, months: int, note: str):
    """Оклад кнопкой за период (пока нет табеля по картам)."""
    out = Decimal(0)
    for l in lines_for(db, user):
        if l.kind != "salary":
            continue
        n = days if l.salary_period == "day" else months
        if n <= 0:
            continue
        amount = money(l.rate * n)
        db.add(PayrollAccrual(user_id=user.id, scheme_line_id=l.id, base_amount=Decimal(n), rate=l.rate, amount=amount,
                              on_date=date.today(), kind="salary", note=note[:300]))
        out += amount
    return out


def accruals(db: Session, user_ids, date_from: date, date_to: date) -> list[PayrollAccrual]:
    return db.scalars(select(PayrollAccrual).where(PayrollAccrual.user_id.in_(list(user_ids)),
                                                   PayrollAccrual.on_date >= date_from, PayrollAccrual.on_date <= date_to,
                                                   PayrollAccrual.status != "cancelled")
                      .order_by(PayrollAccrual.on_date.desc(), PayrollAccrual.created_at.desc())).all()


def describe(line: PaySchemeLine, groups: dict) -> str:
    from ..web import num
    if line.kind == "salary":
        return f"Оклад {num(line.rate)} ₴ {PERIODS.get(line.salary_period, '')}"
    what = f"{num(line.rate)} ₴ за шт" if line.kind == "per_unit" else f"{num(line.rate)} % {BASES.get(line.base, '')}"
    f = []
    if line.filter_group_id:
        f.append(f"группа «{groups.get(line.filter_group_id, '?')}»")
    if line.filter_source:
        f.append(SOURCES[line.filter_source])
    return what + (" · " + ", ".join(f) if f else "")


def parse_uuid(v):
    try:
        return uuid.UUID(v) if v else None
    except ValueError:
        return None


# ---------- Подробная таблица (раздел «Зарплата») ----------
COLS = {"works": "Работы", "parts": "Запчасти", "salary": "Оклад", "other": "Прочее", "deduction": "Удержания"}


def column_of(a: PayrollAccrual) -> str:
    if a.kind == "deduction":
        return "deduction"
    if a.kind == "salary":
        return "salary"
    if a.wo_work_id:
        return "works"
    if a.wo_part_id:
        return "parts"
    return "other"


def payouts(db: Session, user_ids, date_from: date, date_to: date) -> list[PayrollPayout]:
    return db.scalars(select(PayrollPayout).where(PayrollPayout.user_id.in_(list(user_ids)),
                                                  PayrollPayout.paid_on >= date_from, PayrollPayout.paid_on <= date_to)
                      .order_by(PayrollPayout.paid_on.desc(), PayrollPayout.created_at.desc())).all()


def summary(db: Session, users, date_from: date, date_to: date) -> dict:
    """По каждому сотруднику: суммы по колонкам, число нарядов, начислено, выплачено, к выплате."""
    ids = [u.id for u in users]
    out = {uid: {**{k: Decimal(0) for k in COLS}, "orders": set(), "accrued": Decimal(0), "paid": Decimal(0),
                 "due": Decimal(0), "hours": Decimal(0)} for uid in ids}
    for a in accruals(db, ids, date_from, date_to):
        r = out[a.user_id]
        r[column_of(a)] += a.amount
        r["accrued"] += a.amount
        if a.work_order_id:
            r["orders"].add(a.work_order_id)
    for p in payouts(db, ids, date_from, date_to):
        out[p.user_id]["paid"] += p.amount
    # Нормо-часы выполненных работ за период (для слесарей)
    from datetime import datetime, time, timezone
    t1 = datetime.combine(date_from, time.min).astimezone(timezone.utc)
    t2 = datetime.combine(date_to, time.max).astimezone(timezone.utc)
    for w in db.scalars(select(WoWork).where(WoWork.mechanic_id.in_(ids), WoWork.status == "done",
                                             WoWork.done_at >= t1, WoWork.done_at <= t2)):
        out[w.mechanic_id]["hours"] += Decimal(w.hours or 0) * Decimal(w.qty or 1)
    for r in out.values():
        r["due"] = r["accrued"] - r["paid"]
        r["n_orders"] = len(r.pop("orders"))
    return out
