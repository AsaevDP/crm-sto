"""Кабинет директора и заместителя (пункт 8). Директор видит прибыль и бухгалтерию (reports.finance),
заместитель — операционные отчёты: выручка, наряды, загрузка мастеров, склад (reports.view)."""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..models.money import PAY_METHODS
from ..services import orders as osvc
from ..services import reports as svc
from ..web import Ctx, get_ctx

router = APIRouter()
TABS = {"overview": "Обзор", "quarters": "Кварталы", "staff": "Мастера", "accounting": "Бухгалтерия"}


def _p(period: str, y: int, n: int, f: str, t: str):
    return svc.period(period if period in ("month", "quarter", "year", "custom") else "month", y, n, f, t)


def _data(ctx: Ctx, tab: str, p) -> dict:
    db, bid = ctx.db, ctx.branch.id
    fin = ctx.can("reports.finance")
    out = {"fin": fin}
    if tab == "overview":
        cur = svc.money_for(db, bid, p.d1, p.d2)
        months = svc.by_months(db, bid, p.d1.year)
        out.update(m=cur, months=months, funnel=svc.orders_funnel(db, bid),
                   ch=svc.chart(months, ("revenue", "gross") if fin else ("revenue",)),
                   stock=svc.stock_value(db, bid), top=svc.mechanics_load(db, bid, p.d1, p.d2)[:5])
    elif tab == "quarters":
        out.update(qlist=svc.quarters(db, bid, p.d1.year), year=p.d1.year,
                   ytd=svc.money_for(db, bid, date(p.d1.year, 1, 1), min(date(p.d1.year, 12, 31), date.today())))
    elif tab == "staff":
        out.update(load=svc.mechanics_load(db, bid, p.d1, p.d2))
    elif tab == "accounting":
        out.update(m=svc.money_for(db, bid, p.d1, p.d2), debts=svc.debts(db, bid),
                   suppliers=svc.purchases_by_supplier(db, bid, p.d1, p.d2), stock=svc.stock_value(db, bid))
    return out


@router.get("/reports")
def reports(ctx: Ctx = Depends(get_ctx), tab: str = "overview", period: str = "month", y: int = 0, n: int = 0,
            f: str = "", t: str = ""):
    ctx.need("reports.view", "reports.finance")
    if tab not in TABS or (tab == "accounting" and not ctx.can("reports.finance")):
        tab = "overview"
    p = _p(period, y, n, f, t)
    tabs = {k: v for k, v in TABS.items() if k != "accounting" or ctx.can("reports.finance")}
    qs = f"period={p.kind}&y={p.d1.year}&n={p.d1.month if p.kind == 'month' else (p.d1.month - 1) // 3 + 1}"
    if p.kind == "custom":
        qs = f"period=custom&f={p.d1}&t={p.d2}"
    return ctx.render("reports/index.html", title="Кабинет", tab=tab, tabs=tabs, p=p, qs=qs, PAY_METHODS=PAY_METHODS,
                      fmt_plate=osvc.fmt_plate, MONTHS=svc.MONTHS, **_data(ctx, tab, p))


@router.get("/reports/print")
def reports_print(ctx: Ctx = Depends(get_ctx), tab: str = "overview", period: str = "month", y: int = 0, n: int = 0,
                  f: str = "", t: str = ""):
    ctx.need("reports.view", "reports.finance")
    if tab not in TABS or (tab == "accounting" and not ctx.can("reports.finance")):
        tab = "overview"
    p = _p(period, y, n, f, t)
    return ctx.render("reports/print.html", title=f"Отчёт — {TABS[tab]} — {p.label}", tab=tab, tabs=TABS, p=p,
                      PAY_METHODS=PAY_METHODS, MONTHS=svc.MONTHS, **_data(ctx, tab, p))


def _num(x) -> str:
    return str(x).replace(".", ",")


@router.get("/reports/export.csv")
def reports_csv(ctx: Ctx = Depends(get_ctx), tab: str = "overview", period: str = "month", y: int = 0, n: int = 0,
                f: str = "", t: str = ""):
    """Выгрузка в Excel (CSV с разделителем «;» — Excel открывает сразу)."""
    ctx.need("reports.view", "reports.finance")
    fin = ctx.can("reports.finance")
    p = _p(period, y, n, f, t)
    db, bid = ctx.db, ctx.branch.id
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if tab == "quarters":
        w.writerow(["Квартал", "Выручка", "Работы", "Запчасти", "Скидки", "Нарядов", "Средний чек",
                    *(["Себестоимость запчастей", "Зарплата", "Валовая прибыль", "Маржа %"] if fin else [])])
        for q in svc.quarters(db, bid, p.d1.year):
            if q["future"]:
                continue
            r = q["r"]
            w.writerow([f"{q['q']} кв. {p.d1.year}", _num(r.revenue), _num(r.works), _num(r.parts), _num(r.discount),
                        r.orders_paid, _num(r.avg_check),
                        *([_num(r.parts_cost), _num(r.payroll), _num(r.gross), r.margin_pct] if fin else [])])
    elif tab == "staff":
        w.writerow(["Мастер", "Работ выполнено", "Нормо-часов", "Сумма работ", "Нарядов"])
        for x in svc.mechanics_load(db, bid, p.d1, p.d2):
            w.writerow([x["u"].full_name, x["works"], _num(x["hours"]), _num(x["sum"]), x["orders"]])
    elif tab == "accounting" and fin:
        w.writerow(["Долги клиентов"])
        w.writerow(["Наряд", "Клиент", "Итого", "Оплачено", "Долг"])
        for d in svc.debts(db, bid):
            w.writerow([d["wo"].number, d["c"].full_name if d["c"] else "", _num(d["t"].total), _num(d["t"].paid), _num(d["t"].due)])
        w.writerow([])
        w.writerow(["Закупки по поставщикам", p.label])
        w.writerow(["Поставщик", "Закупок", "Сумма"])
        for s in svc.purchases_by_supplier(db, bid, p.d1, p.d2):
            w.writerow([s["name"], s["n"], _num(s["sum"])])
    else:
        w.writerow(["Месяц", "Выручка (пришло денег)", "По нарядам (оплачено)", "Нарядов", *(["Валовая прибыль"] if fin else [])])
        for r in svc.by_months(db, bid, p.d1.year):
            if not r["future"]:
                w.writerow([f"{r['m']} {p.d1.year}", _num(r["revenue"]), _num(r["billed"]), r.get("orders", 0),
                            *([_num(r["gross"])] if fin else [])])
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="otchet_{tab}_{p.d1}_{p.d2}.csv"'})
