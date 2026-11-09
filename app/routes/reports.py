"""Кабинет директора и заместителя (пункт 8). Директор видит прибыль и бухгалтерию (reports.finance),
заместитель — операционные отчёты: выручка, наряды, загрузка мастеров, склад (reports.view)."""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..models.money import PAY_METHODS
from ..services import orders as osvc
from ..services import reports as svc
from ..web import Ctx, get_ctx

router = APIRouter()
# Кабинет (10.11.2026): два раздела — СТО и Разбор; у СТО отдельно «Выполненные работы» и «Проданные запчасти»
SECTIONS = {"sto": "СТО", "razbor": "Разбор"}
TABS = {"overview": "Обзор", "works": "Выполненные работы", "parts": "Проданные запчасти", "staff": "Мастера",
        "quarters": "Кварталы", "accounting": "Бухгалтерия", "razbor": "Продажи б/у", "donors": "Доноры"}
TAB_SECTION = {"razbor": "razbor", "donors": "razbor"}
DOC_TABS = ("works", "parts", "razbor", "donors")   # отчёты-документы: печать с правкой и Excel


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
    elif tab in DOC_TABS:
        from ..services import cabinet as cab
        out.update(rep=cab.BUILDERS[tab](db, bid, p.d1, p.d2, fin))
    return out


def _tabs(ctx: Ctx, sec: str) -> dict:
    return {k: v for k, v in TABS.items() if TAB_SECTION.get(k, "sto") == sec
            and (k != "accounting" or ctx.can("reports.finance"))}


def _qs(p) -> str:
    if p.kind == "custom":
        return f"period=custom&f={p.d1}&t={p.d2}"
    return f"period={p.kind}&y={p.d1.year}&n={p.d1.month if p.kind == 'month' else (p.d1.month - 1) // 3 + 1}"


@router.get("/reports")
def reports(ctx: Ctx = Depends(get_ctx), tab: str = "", sec: str = "", period: str = "month", y: int = 0, n: int = 0,
            f: str = "", t: str = ""):
    ctx.need("reports.view", "reports.finance")
    if tab not in TABS or (tab == "accounting" and not ctx.can("reports.finance")):
        tab = "razbor" if sec == "razbor" else "overview"
    sec = TAB_SECTION.get(tab, "sto")
    p = _p(period, y, n, f, t)
    return ctx.render("reports/index.html", title="Кабинет", tab=tab, sec=sec, sections=SECTIONS, tabs=_tabs(ctx, sec),
                      p=p, qs=_qs(p), PAY_METHODS=PAY_METHODS, fmt_plate=osvc.fmt_plate, MONTHS=svc.MONTHS,
                      DOC_TABS=DOC_TABS, **_data(ctx, tab, p))


@router.get("/reports/doc")
def reports_doc(ctx: Ctx = Depends(get_ctx), tab: str = "works", period: str = "month", y: int = 0, n: int = 0,
                f: str = "", t: str = ""):
    """Отчётный документ: шапка с реквизитами и подписи берутся из шаблона, всё можно поправить на листе перед печатью."""
    ctx.need("reports.view", "reports.finance")
    if tab not in DOC_TABS:
        tab = "works"
    from ..services import settings as st
    p = _p(period, y, n, f, t)
    data = _data(ctx, tab, p)
    return ctx.render("reports/doc.html", title=f"{TABS[tab]} — {p.label}", tab=tab, p=p, qs=_qs(p),
                      tpl=st.get(ctx.db, ctx.branch.id, "report_doc"), **data)


@router.post("/reports/template")
async def reports_template(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Сохранить шапку, подвал и подписи документа как шаблон — подставятся во все следующие отчёты."""
    ctx.need("reports.view", "reports.finance")
    from ..services import settings as st
    data = await request.json()
    st.put(ctx.db, ctx.branch.id, "report_doc", {k: str(data.get(k, ""))[:2000] for k in ("header", "footer", "signer1", "signer2")})
    ctx.db.commit()
    return {"ok": True}


@router.get("/reports/export.xlsx")
def reports_xlsx(ctx: Ctx = Depends(get_ctx), tab: str = "works", period: str = "month", y: int = 0, n: int = 0,
                 f: str = "", t: str = ""):
    """Отчёт-документ в Excel (.xlsx) — браузер предложит сохранить файл на ПК."""
    ctx.need("reports.view", "reports.finance")
    if tab not in DOC_TABS:
        tab = "works"
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    p = _p(period, y, n, f, t)
    rep = _data(ctx, tab, p)["rep"]
    wb = Workbook()
    ws = wb.active
    ws.title = TABS[tab][:31]
    ws.append([f"{rep.title} — {p.label}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append([c[1] for c in rep.columns])
    for c in ws[3]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEEEF2")
    for r in rep.rows:
        ws.append([float(r[k]) if kind in ("num", "money") else r[k] for k, _, kind in rep.columns])
    total_row = ["Итого"] + ["" for _ in rep.columns[1:]]
    for i, (k, _, kind) in enumerate(rep.columns):
        if kind and k in rep.totals and i:
            total_row[i] = float(rep.totals[k])
    ws.append(total_row)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for i, (k, _, kind) in enumerate(rep.columns, start=1):
        letter = ws.cell(row=3, column=i).column_letter
        ws.column_dimensions[letter].width = 14 if kind else max(12, min(40, max((len(str(r[k])) for r in rep.rows), default=10) + 2))
        if kind == "money":
            for row in ws.iter_rows(min_row=4, min_col=i, max_col=i):
                row[0].number_format = '# ##0.00'
                row[0].alignment = Alignment(horizontal="right")
    ws.freeze_panes = "A4"
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="otchet_{tab}_{p.d1}_{p.d2}.xlsx"'})


@router.get("/reports/print")
def reports_print(ctx: Ctx = Depends(get_ctx), tab: str = "overview", period: str = "month", y: int = 0, n: int = 0,
                  f: str = "", t: str = ""):
    ctx.need("reports.view", "reports.finance")
    if tab in DOC_TABS:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/reports/doc?tab={tab}&{_qs(_p(period, y, n, f, t))}", status_code=303)
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
