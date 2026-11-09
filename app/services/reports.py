"""Кабинет директора / заместителя (пункт 8): отчёты, графики, кварталы, бухгалтерия.

Выручка — деньги, которые реально пришли (оплаты по нарядам + продажи б/у) за период.
Работы/запчасти/себестоимость/прибыль — по нарядам, оплаченным в периоде (paid_at).
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Client, Payment, PayrollAccrual, PayrollPayout, Purchase, PurchaseLine, StockItem, Supplier,
                      UsedSale, User, WoPart, WorkOrder, WoWork)
from ..models.money import PAY_METHODS
from .money import money, totals
from .schedule import day_range

D0 = Decimal(0)
MONTHS = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
MONTHS_FULL = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
               "ноябрь", "декабрь"]


# ---------- Период ----------
@dataclass
class Period:
    kind: str       # month / quarter / year / custom
    d1: date
    d2: date
    label: str
    prev: str = ""  # параметры ссылки «назад/вперёд»
    next: str = ""


def _month_end(y, m):
    return (date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1))


def period(kind: str = "month", y: int = 0, n: int = 0, f: str = "", t: str = "") -> Period:
    today = date.today()
    y = y or today.year
    if kind == "custom":
        try:
            d1, d2 = date.fromisoformat(f), date.fromisoformat(t)
        except ValueError:
            d1, d2 = today.replace(day=1), today
        return Period("custom", min(d1, d2), max(d1, d2), f"{min(d1, d2):%d.%m.%Y} — {max(d1, d2):%d.%m.%Y}")
    if kind == "year":
        return Period("year", date(y, 1, 1), date(y, 12, 31), f"{y} год",
                      f"period=year&y={y - 1}", f"period=year&y={y + 1}")
    if kind == "quarter":
        q = n or (today.month - 1) // 3 + 1
        d1 = date(y, 3 * q - 2, 1)
        py, pq = (y - 1, 4) if q == 1 else (y, q - 1)
        ny, nq = (y + 1, 1) if q == 4 else (y, q + 1)
        return Period("quarter", d1, _month_end(y, 3 * q), f"{q} квартал {y}",
                      f"period=quarter&y={py}&n={pq}", f"period=quarter&y={ny}&n={nq}")
    m = n or today.month
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return Period("month", date(y, m, 1), _month_end(y, m), f"{MONTHS_FULL[m - 1].capitalize()} {y}",
                  f"period=month&y={py}&n={pm}", f"period=month&y={ny}&n={nm}")


# ---------- Деньги за период ----------
@dataclass
class Money:
    revenue: Decimal = D0                         # пришло денег всего
    by_method: dict = field(default_factory=dict)
    used_sales: Decimal = D0                      # из них разбор (б/у)
    orders_paid: int = 0                          # нарядов оплачено (paid_at в периоде)
    works: Decimal = D0                           # сумма работ по этим нарядам
    parts: Decimal = D0                           # сумма запчастей (продажа)
    discount: Decimal = D0
    billed: Decimal = D0                          # итог по этим нарядам
    parts_cost: Decimal = D0                      # себестоимость запчастей
    used_cost: Decimal = D0                       # себестоимость б/у (не считаем: донор окупается отдельно)
    payroll: Decimal = D0                         # начислено зарплаты за период
    payroll_paid: Decimal = D0
    purchases: Decimal = D0                       # закуплено у поставщиков
    orders_created: int = 0
    orders_closed: int = 0

    @property
    def avg_check(self) -> Decimal:
        return money(self.billed / self.orders_paid) if self.orders_paid else D0

    @property
    def gross(self) -> Decimal:
        """Валовая прибыль: продажи по нарядам − себестоимость запчастей − зарплата + разбор."""
        return money(self.billed - self.parts_cost - self.payroll + self.used_sales)

    @property
    def margin_pct(self) -> int:
        base = self.billed + self.used_sales
        return round(100 * self.gross / base) if base else 0


def money_for(db: Session, bid, d1: date, d2: date, light: bool = False) -> Money:
    s, e = day_range(d1, d2)
    r = Money(by_method={k: D0 for k in PAY_METHODS})
    for method, amount in db.execute(select(Payment.method, func.sum(Payment.amount)).where(
            Payment.branch_id == bid, Payment.created_at >= s, Payment.created_at < e).group_by(Payment.method)):
        r.by_method[method] = r.by_method.get(method, D0) + Decimal(amount or 0)
    for method, amount in db.execute(select(UsedSale.pay_method, func.sum(UsedSale.price)).where(
            UsedSale.branch_id == bid, UsedSale.status == "sold", UsedSale.sold_at >= s, UsedSale.sold_at < e)
            .group_by(UsedSale.pay_method)):
        r.by_method[method] = r.by_method.get(method, D0) + Decimal(amount or 0)
        r.used_sales += Decimal(amount or 0)
    r.revenue = money(sum(r.by_method.values(), D0))
    paid_orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.paid_at >= s,
                                                     WorkOrder.paid_at < e, WorkOrder.status.in_(("paid", "closed")))).all()
    r.orders_paid = len(paid_orders)
    for wo in paid_orders:
        t = totals(db, wo)
        r.works += t.works
        r.parts += t.parts
        r.discount += t.discount
        r.billed += t.total
    if paid_orders:
        r.parts_cost = money(sum((Decimal(p.cost or 0) * Decimal(p.qty or 0) for p in db.scalars(select(WoPart).where(
            WoPart.work_order_id.in_([o.id for o in paid_orders]), WoPart.status != "returned"))), D0))
    r.payroll = money(db.scalar(select(func.sum(PayrollAccrual.amount)).where(
        PayrollAccrual.branch_id == bid, PayrollAccrual.status != "cancelled", PayrollAccrual.on_date >= d1,
        PayrollAccrual.on_date <= d2)) or 0)
    if light:
        return r
    r.payroll_paid = money(db.scalar(select(func.sum(PayrollPayout.amount)).where(
        PayrollPayout.branch_id == bid, PayrollPayout.paid_on >= d1, PayrollPayout.paid_on <= d2)) or 0)
    r.purchases = money(db.scalar(select(func.sum(PurchaseLine.price * PurchaseLine.qty))
                                  .join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                                  .where(Purchase.branch_id == bid, Purchase.status != "draft",
                                         Purchase.created_at >= s, Purchase.created_at < e)) or 0)
    r.orders_created = db.scalar(select(func.count()).select_from(WorkOrder).where(
        WorkOrder.branch_id == bid, WorkOrder.created_at >= s, WorkOrder.created_at < e)) or 0
    r.orders_closed = db.scalar(select(func.count()).select_from(WorkOrder).where(
        WorkOrder.branch_id == bid, WorkOrder.closed_at >= s, WorkOrder.closed_at < e)) or 0
    return r


# ---------- График по месяцам года ----------
def by_months(db: Session, bid, year: int) -> list[dict]:
    today = date.today()
    out = []
    for m in range(1, 13):
        d1 = date(year, m, 1)
        if d1 > today:
            out.append({"m": MONTHS[m - 1], "n": m, "revenue": D0, "gross": D0, "billed": D0, "future": True})
            continue
        r = money_for(db, bid, d1, _month_end(year, m), light=True)
        out.append({"m": MONTHS[m - 1], "n": m, "revenue": r.revenue, "gross": r.gross, "billed": r.billed,
                    "orders": r.orders_paid, "future": False})
    return out


def chart(rows: list[dict], keys=("revenue",), w: int = 640, h: int = 200) -> dict:
    """Столбики для SVG: координаты считаем здесь, шаблон только рисует."""
    vmax = max([Decimal(1)] + [abs(r[k]) for r in rows for k in keys])
    # Красивая верхняя граница шкалы
    mag = Decimal(10) ** (len(str(int(vmax))) - 1)
    top = ((vmax / mag).to_integral_value(rounding="ROUND_CEILING")) * mag
    pad_l, pad_b, pad_t = 44, 22, 8
    inner_w, inner_h = w - pad_l - 4, h - pad_b - pad_t
    n = len(rows)
    slot = inner_w / n
    bw = slot * (0.62 if len(keys) == 1 else 0.36)
    bars = []
    for i, r in enumerate(rows):
        for j, k in enumerate(keys):
            v = r[k]
            bh = float(abs(v) / top) * inner_h
            x = pad_l + i * slot + (slot - bw * len(keys)) / 2 + j * bw
            y = pad_t + inner_h - (bh if v >= 0 else 0)
            bars.append({"x": round(x, 1), "y": round(y, 1), "w": round(bw - 2, 1), "h": round(max(bh, 0 if not v else 1.5), 1),
                         "k": k, "v": v, "label": r["m"], "neg": v < 0})
    ticks = [{"y": round(pad_t + inner_h - inner_h * i / 4, 1), "v": top * i / 4} for i in range(5)]
    labels = [{"x": round(pad_l + i * slot + slot / 2, 1), "t": r["m"]} for i, r in enumerate(rows)]
    return {"w": w, "h": h, "bars": bars, "ticks": ticks, "labels": labels, "base": pad_t + inner_h, "left": pad_l}


# ---------- Кварталы ----------
def quarters(db: Session, bid, year: int) -> list[dict]:
    today = date.today()
    out = []
    for q in range(1, 5):
        d1 = date(year, 3 * q - 2, 1)
        if d1 > today:
            out.append({"q": q, "future": True})
            continue
        r = money_for(db, bid, d1, _month_end(year, 3 * q))
        out.append({"q": q, "future": False, "r": r})
    return out


# ---------- Мастера и сотрудники ----------
def mechanics_load(db: Session, bid, d1: date, d2: date) -> list[dict]:
    s, e = day_range(d1, d2)
    from ..models import Role
    rows = {}
    for w in db.scalars(select(WoWork).where(WoWork.branch_id == bid, WoWork.status == "done",
                                             WoWork.done_at >= s, WoWork.done_at < e)):
        uid = w.mechanic_id
        if not uid:
            continue
        r = rows.setdefault(uid, {"works": 0, "hours": D0, "sum": D0, "orders": set()})
        r["works"] += 1
        r["hours"] += Decimal(w.hours or 0) * Decimal(w.qty or 1)
        r["sum"] += Decimal(w.price or 0)
        r["orders"].add(w.work_order_id)
    users = db.scalars(select(User).join(Role, User.role_id == Role.id).where(
        User.branch_id == bid, User.active.is_(True), Role.code == "mechanic").order_by(User.full_name)).all()
    out = []
    for u in users:
        r = rows.get(u.id, {"works": 0, "hours": D0, "sum": D0, "orders": set()})
        out.append({"u": u, "works": r["works"], "hours": r["hours"], "sum": money(r["sum"]), "orders": len(r["orders"])})
    best = max([x["sum"] for x in out] + [D0])
    for x in out:
        x["pct"] = round(100 * x["sum"] / best) if best else 0
    return out


def orders_funnel(db: Session, bid) -> list[tuple]:
    from ..models.orders import WO_ACTIVE, WO_STATUSES
    counts = dict(db.execute(select(WorkOrder.status, func.count()).where(
        WorkOrder.branch_id == bid, WorkOrder.status.in_(WO_ACTIVE)).group_by(WorkOrder.status)).all())
    return [(WO_STATUSES[s], counts.get(s, 0)) for s in WO_ACTIVE]


# ---------- Бухгалтерия ----------
def debts(db: Session, bid) -> list[dict]:
    out = []
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(("ready", "paid", "closed")))):
        t = totals(db, wo)
        if t.due > 0:
            out.append({"wo": wo, "t": t, "c": db.get(Client, wo.client_id)})
    return sorted(out, key=lambda x: -x["t"].due)


def purchases_by_supplier(db: Session, bid, d1: date, d2: date) -> list[dict]:
    s, e = day_range(d1, d2)
    rows = db.execute(select(Purchase.supplier_id, func.count(func.distinct(Purchase.id)),
                             func.sum(PurchaseLine.price * PurchaseLine.qty))
                      .join(PurchaseLine, PurchaseLine.purchase_id == Purchase.id)
                      .where(Purchase.branch_id == bid, Purchase.status != "draft",
                             Purchase.created_at >= s, Purchase.created_at < e)
                      .group_by(Purchase.supplier_id)).all()
    names = {x.id: x.name for x in db.scalars(select(Supplier).where(Supplier.branch_id == bid))}
    return sorted([{"name": names.get(sid, "Без поставщика"), "n": n, "sum": money(total or 0)} for sid, n, total in rows],
                  key=lambda x: -x["sum"])


def stock_value(db: Session, bid) -> dict:
    """Склад в деньгах: новые — по закупке, б/у — по цене продажи (сколько можно выручить)."""
    from ..models import PartCatalog
    from .stock import balances
    bal = balances(db, bid)
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.branch_id == bid))}
    out = {"new": D0, "used": D0, "positions": 0, "by_section": {"to": D0, "repair": D0}}
    for i in db.scalars(select(StockItem).where(StockItem.branch_id == bid)):
        q = Decimal(bal[i.id]["on_hand"])
        if q <= 0:
            continue
        c = cats.get(i.catalog_id)
        out["positions"] += 1
        if c and c.kind == "used":
            out["used"] += q * Decimal(i.sale_price or 0)
        else:
            v = q * Decimal(i.cost or 0)
            out["new"] += v
            if c and c.section in out["by_section"]:
                out["by_section"][c.section] += v
    return {k: (money(v) if isinstance(v, Decimal) else v) for k, v in out.items()}
