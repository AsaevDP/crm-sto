"""Кабинет (10.11.2026): отчёты «СТО → Выполненные работы», «СТО → Проданные запчасти», «Разбор → Продажи»,
«Разбор → Доноры». Одни и те же строки идут на экран, в документ для печати и в Excel."""
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Donor, PartCatalog, StockItem, User, Vehicle, WorkOrder, WoPart, WoWork
from ..models.razbor import DELIVERY, UsedSale
from ..models.money import PAY_METHODS
from . import orders as osvc
from .money import money
from .reports import day_range
from ..models.orders import PART_OFF

D0 = Decimal(0)


@dataclass
class Report:
    kind: str
    title: str
    columns: list                      # [(ключ, подпись, «num» / «money» / «»)]
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)   # сводка: [(подпись, значение, доп.)]


def _local(dt):
    return dt.astimezone() if dt else None


def works(db: Session, bid, d1, d2, fin: bool) -> Report:
    """Выполненные работы за период: по дате «Выполнено»."""
    s, e = day_range(d1, d2)
    ws = db.scalars(select(WoWork).where(WoWork.branch_id == bid, WoWork.status == "done", WoWork.done_at >= s,
                                         WoWork.done_at < e).order_by(WoWork.done_at)).all()
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_({w.work_order_id for w in ws})))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_({o.vehicle_id for o in orders.values()})))}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == bid))}
    r = Report("works", "Выполненные работы", [
        ("date", "Дата", ""), ("order", "Наряд", ""), ("car", "Автомобиль", ""), ("name", "Работа", ""),
        ("mech", "Слесарь", ""), ("hours", "Н/ч", "num"), ("sum", "Сумма, ₴", "money")])
    by_mech: dict = {}
    for w in ws:
        o = orders.get(w.work_order_id)
        v = vehicles.get(o.vehicle_id) if o else None
        m = people.get(w.mechanic_id)
        hours = Decimal(w.hours or 0) * Decimal(w.qty or 1) if w.hours else D0
        total = money(Decimal(w.price or 0))
        r.rows.append({"date": _local(w.done_at).strftime("%d.%m.%Y"), "order": o.number if o else "",
                       "order_id": str(o.id) if o else "",
                       "car": f"{v.make} {v.model} {osvc.fmt_plate(v.plate)}".strip() if v else "",
                       "name": w.name, "mech": m.full_name if m else "—", "hours": hours, "sum": total})
        g = by_mech.setdefault(m.full_name if m else "—", [0, D0, D0])
        g[0] += 1
        g[1] += hours
        g[2] += total
    r.totals = {"count": len(r.rows), "hours": sum((x["hours"] for x in r.rows), D0),
                "sum": sum((x["sum"] for x in r.rows), D0)}
    r.groups = sorted(((k, v[2], f"{v[0]} работ · {v[1].normalize():f} н/ч") for k, v in by_mech.items()),
                      key=lambda x: -x[1])
    return r


def parts(db: Session, bid, d1, d2, fin: bool) -> Report:
    """Проданные запчасти: из нарядов, оплаченных за период (как «Пришло денег по нарядам» в обзоре)."""
    s, e = day_range(d1, d2)
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(
        WorkOrder.branch_id == bid, WorkOrder.paid_at >= s, WorkOrder.paid_at < e,
        WorkOrder.status.in_(("paid", "closed"))))}
    ps = db.scalars(select(WoPart).where(WoPart.work_order_id.in_(list(orders)), WoPart.status.notin_(PART_OFF))
                    .order_by(WoPart.created_at)).all() if orders else []
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_({p.catalog_id for p in ps if p.catalog_id})))}
    cols = [("date", "Дата оплаты", ""), ("order", "Наряд", ""), ("name", "Запчасть", ""), ("article", "Артикул / бренд", ""),
            ("qty", "Кол-во", "num")]
    if fin:
        cols += [("cost", "Закупка, ₴", "money")]
    cols += [("price", "Цена, ₴", "money"), ("sum", "Сумма, ₴", "money")]
    if fin:
        cols += [("margin", "Наценка, ₴", "money")]
    r = Report("parts", "Проданные запчасти", cols)
    new_sum = used_sum = D0
    for p in ps:
        o = orders[p.work_order_id]
        c = cats.get(p.catalog_id)
        qty = Decimal(p.qty or 0)
        total = money(Decimal(p.price or 0) * qty)
        cost = money(Decimal(p.cost or 0) * qty)
        used = bool(c and c.kind == "used")
        if used:
            used_sum += total
        else:
            new_sum += total
        r.rows.append({"date": _local(o.paid_at).strftime("%d.%m.%Y"), "order": o.number, "order_id": str(o.id),
                       "name": p.name + (" (б/у)" if used else ""),
                       "article": " ".join(x for x in ((c.article if c else ""), (c.brand if c else "")) if x),
                       "qty": qty, "cost": cost, "price": money(Decimal(p.price or 0)), "sum": total, "margin": total - cost})
    r.totals = {"count": len(r.rows), "qty": sum((x["qty"] for x in r.rows), D0),
                "sum": sum((x["sum"] for x in r.rows), D0), "cost": sum((x["cost"] for x in r.rows), D0),
                "margin": sum((x["margin"] for x in r.rows), D0)}
    r.groups = [("Новые запчасти", new_sum, ""), ("Б/у с разбора", used_sum, "")]
    return r


def razbor_sales(db: Session, bid, d1, d2, fin: bool) -> Report:
    """Продажи б/у деталей покупателям (не через наряды) за период."""
    s, e = day_range(d1, d2)
    sales = db.scalars(select(UsedSale).where(UsedSale.branch_id == bid, UsedSale.status == "sold",
                                              UsedSale.sold_at >= s, UsedSale.sold_at < e).order_by(UsedSale.sold_at)).all()
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.id.in_({x.stock_item_id for x in sales})))}
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_({i.catalog_id for i in items.values()})))}
    cols = [("date", "Дата", ""), ("code", "Код", ""), ("name", "Деталь", ""), ("donor", "Донор", ""),
            ("buyer", "Покупатель", ""), ("delivery", "Доставка / оплата", ""), ("sum", "Сумма, ₴", "money")]
    r = Report("razbor", "Продажи разбора (б/у)", cols)
    by_pay: dict = {}
    for x in sales:
        i = items.get(x.stock_item_id)
        c = cats.get(i.catalog_id) if i else None
        total = money(Decimal(x.price or 0))
        r.rows.append({"date": _local(x.sold_at).strftime("%d.%m.%Y"), "code": i.code if i else "",
                       "name": c.name if c else "", "donor": " ".join(z for z in ((i.donor_make if i else ""), (i.donor_model if i else "")) if z),
                       "buyer": x.buyer_name or "—",
                       "delivery": f"{DELIVERY.get(x.delivery, x.delivery)}{(' · ТТН ' + x.ttn) if x.ttn else ''} · {PAY_METHODS.get(x.pay_method, x.pay_method)}",
                       "sum": total})
        by_pay[PAY_METHODS.get(x.pay_method, x.pay_method)] = by_pay.get(PAY_METHODS.get(x.pay_method, x.pay_method), D0) + total
    r.totals = {"count": len(r.rows), "sum": sum((x["sum"] for x in r.rows), D0)}
    r.groups = sorted(((k, v, "") for k, v in by_pay.items()), key=lambda z: -z[1])
    return r


def donors(db: Session, bid, d1, d2, fin: bool) -> Report:
    """Доноры за всё время: сколько вложили (покупка + расходы), сколько выручили, что осталось на складе."""
    from ..models.razbor import DONOR_STATUS
    from .stock import balances
    ds = db.scalars(select(Donor).where(Donor.branch_id == bid).order_by(Donor.bought_on.desc())).all()
    bal = balances(db, bid)

    def qty_free(db_, i):
        return bal[i.id]["free"] if i.id in bal else D0
    cols = [("code", "Код", ""), ("car", "Автомобиль", ""), ("status", "Статус", ""), ("spent", "Вложено, ₴", "money"),
            ("sold", "Продано, ₴", "money"), ("left", "Осталось деталей", "num"), ("left_sum", "На складе на, ₴", "money"),
            ("profit", "Итог, ₴", "money")]
    r = Report("donors", "Доноры: вложено и выручено", cols)
    for d in ds:
        items = db.scalars(select(StockItem).where(StockItem.donor_id == d.id)).all()
        sold = money(sum((Decimal(x.price or 0) for x in db.scalars(select(UsedSale).where(
            UsedSale.stock_item_id.in_([i.id for i in items]), UsedSale.status == "sold"))), D0)) if items else D0
        # Б/у, ушедшие в наряды клиентам, тоже выручка донора
        in_orders = D0
        if items:
            for p in db.scalars(select(WoPart).where(WoPart.catalog_id.in_([i.catalog_id for i in items]),
                                                     WoPart.status.notin_(PART_OFF))):
                in_orders += Decimal(p.price or 0) * Decimal(p.qty or 0)
            in_orders = money(in_orders)
        left = [i for i in items if qty_free(db, i) > 0] if items else []
        spent = money(Decimal(d.purchase_price or 0) + Decimal(d.expenses or 0))
        left_sum = money(sum((Decimal(i.sale_price or 0) * qty_free(db, i) for i in left), D0))
        r.rows.append({"code": d.code, "car": f"{d.make} {d.model}{(', ' + str(d.year)) if d.year else ''}",
                       "status": DONOR_STATUS.get(d.status, d.status), "spent": spent, "sold": sold + in_orders,
                       "left": len(left), "left_sum": left_sum, "profit": sold + in_orders - spent})
    r.totals = {"count": len(r.rows), "spent": sum((x["spent"] for x in r.rows), D0),
                "sold": sum((x["sold"] for x in r.rows), D0), "left_sum": sum((x["left_sum"] for x in r.rows), D0),
                "profit": sum((x["profit"] for x in r.rows), D0)}
    return r


BUILDERS = {"works": works, "parts": parts, "razbor": razbor_sales, "donors": donors}
