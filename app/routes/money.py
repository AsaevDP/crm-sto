"""Шаг 6: работы и цены, кнопки слесаря, оплаты, закрытие, печать, раздел «Деньги», зарплата."""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..db import now
from ..models import (Client, PartCatalog, PartGroup, PartGroupItem, Payment, PayrollAccrual, PaySchemeLine, User,
                      Vehicle, WoPart, WorkOrder, WoWork, WoWorkSession)
from ..models.money import PAY_METHODS, PAY_PURPOSES
from ..models.orders import WO_STATUSES
from ..services import money as msvc
from ..services import orders as osvc
from ..services import payroll as psvc
from ..services import settings as st
from ..services.staff import staff_for_period
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid
from ..models.orders import PART_OFF

router = APIRouter()


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return Decimal(default)


def _to(url):
    return RedirectResponse(url, status_code=303)


def _is_admin(ctx: Ctx) -> bool:
    return ctx.user.role.code == "admin"


EDITABLE = ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready")


# ======================= Работы и цены =======================
def _apply_work(ctx: Ctx, w: WoWork, name, pricing, hours, rate, qty, mechanic_id):
    w.name = name.strip()[:200] or w.name
    w.pricing = "fixed" if pricing == "fixed" else "hours"
    w.hours = None if w.pricing == "fixed" else _dec(hours)
    w.rate = msvc.money(_dec(rate))
    w.qty = max(_dec(qty, "1"), Decimal("0.01"))
    if mechanic_id is not None:
        u = ctx.db.get(User, uuid.UUID(mechanic_id)) if mechanic_id else None
        w.mechanic_id = u.id if u and u.branch_id == ctx.branch.id and u.role.code == "mechanic" else None
    w.price = msvc.work_price(w)


@router.post("/orders/{order_id}/works")
def work_add(order_id: str, ctx: Ctx = Depends(get_ctx), name: str = Form(...), pricing: str = Form("hours"),
             hours: str = Form("1"), rate: str = Form(""), qty: str = Form("1"), mechanic_id: str = Form("")):
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    if wo.status not in EDITABLE or not name.strip():
        raise Forbidden()
    w = WoWork(work_order_id=wo.id, name=name, status="todo")
    if not rate:
        rate = str(st.get(ctx.db, ctx.branch.id, "pricing")["hour_rate"]) if pricing != "fixed" else "0"
    _apply_work(ctx, w, name, pricing, hours, rate, qty, mechanic_id or (str(wo.mechanic_id) if wo.mechanic_id else ""))
    ctx.db.add(w)
    ctx.db.commit()
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/works/{work_id}")
def work_edit(order_id: str, work_id: str, ctx: Ctx = Depends(get_ctx), name: str = Form(""), pricing: str = Form("hours"),
              hours: str = Form("0"), rate: str = Form("0"), qty: str = Form("1"), mechanic_id: str = Form(None),
              delete: str = Form("")):
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    w = ctx.db.get(WoWork, _uuid(work_id))
    if not w or w.work_order_id != wo.id or wo.status not in EDITABLE:
        raise Forbidden()
    if delete:
        if w.status == "done":
            flash(ctx.request, "Выполненную работу удалить нельзя", "error")
            return _to(f"/orders/{order_id}")
        for p in ctx.db.scalars(select(WoPart).where(WoPart.wo_work_id == w.id)):
            p.wo_work_id = None
        for s in ctx.db.scalars(select(WoWorkSession).where(WoWorkSession.wo_work_id == w.id)):
            ctx.db.delete(s)
        ctx.db.delete(w)
    else:
        _apply_work(ctx, w, name, pricing, hours, rate, qty, mechanic_id)
    ctx.db.commit()
    if ctx.request.headers.get("x-requested-with") == "fetch":
        t = msvc.totals(ctx.db, wo)
        return JSONResponse({"ok": True, "price": str(w.price) if not delete else "0", "total": str(t.total), "due": str(t.due)})
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/works/{work_id}/state")
def work_state(order_id: str, work_id: str, ctx: Ctx = Depends(get_ctx), action: str = Form(...), reason: str = Form("")):
    """Слесарь: Начал / Пауза (с причиной) / Продолжить / Готово. Отрезки времени → пульт и реальное время работ."""
    wo = _order(ctx, order_id)
    w = ctx.db.get(WoWork, _uuid(work_id))
    # «Вернуть» (снять отметку «Готово») — менеджер, пока машину не выдали
    undo_ok = action == "undo" and ctx.can("orders.view_all") and wo.status in ("work", "ready")
    if not w or w.work_order_id != wo.id or (wo.status != "work" and not undo_ok):
        raise Forbidden()
    if not (ctx.user.id in (w.mechanic_id, wo.mechanic_id) or ctx.can("orders.view_all")):
        raise Forbidden()
    db, t = ctx.db, now()
    mech = ctx.user.id if ctx.user.role.code == "mechanic" else (w.mechanic_id or wo.mechanic_id)

    def close_open():
        for s in db.scalars(select(WoWorkSession).where(WoWorkSession.wo_work_id == w.id, WoWorkSession.ended_at.is_(None))):
            s.ended_at = t

    if action == "start" and w.status in ("todo", "paused"):
        close_open()
        db.add(WoWorkSession(work_order_id=wo.id, wo_work_id=w.id, mechanic_id=mech, lift_id=wo.lift_id, started_at=t))
        w.status = "doing"
    elif action == "pause" and w.status == "doing":
        close_open()
        db.add(WoWorkSession(work_order_id=wo.id, wo_work_id=w.id, mechanic_id=mech, lift_id=wo.lift_id, started_at=t,
                             kind="pause", pause_reason=(reason.strip() or "Без причины")[:200]))
        w.status = "paused"
    elif action == "done" and w.status in ("todo", "doing", "paused"):
        close_open()
        w.status, w.done_at, w.done_by = "done", t, ctx.user.id
        db.flush()
        left = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.status != "done")).all()
        if not left:
            osvc.set_status(db, wo, "ready", "Все работы выполнены")
            msvc.mark_paid_if_settled(db, wo)
    elif undo_ok and w.status == "done":
        w.status, w.done_at, w.done_by = "todo", None, None
        if wo.status == "ready":
            osvc.set_status(db, wo, "work", f"Работа «{w.name[:60]}» возвращена в работу")
    db.commit()
    return _to(f"/orders/{order_id}")


# ======================= Оплата, скидка, предоплата =======================
@router.post("/orders/{order_id}/money")
def order_money(order_id: str, ctx: Ctx = Depends(get_ctx), payment_type: str = Form("post"), prepay_amount: str = Form("0"),
                prepay_for: str = Form(""), discount_pct: str = Form("0"), payroll_ignore_discount: str = Form(None)):
    ctx.need("money.accept")
    wo = _order(ctx, order_id)
    if wo.status in ("paid", "closed", "cancel"):
        raise Forbidden()
    wo.payment_type = "prepay" if payment_type == "prepay" else "post"
    wo.prepay_amount = msvc.money(_dec(prepay_amount)) if wo.payment_type == "prepay" else Decimal(0)
    wo.prepay_for = prepay_for.strip()[:500] if wo.payment_type == "prepay" else ""
    wo.discount_pct = max(Decimal(0), min(Decimal(100), _dec(discount_pct)))
    if payroll_ignore_discount is not None and _is_admin(ctx):   # только главный админ
        wo.payroll_ignore_discount = payroll_ignore_discount == "1"
    msvc.refresh_prepay(ctx.db, wo)
    ctx.db.commit()
    flash(ctx.request, "Сохранено" + (" — подбор заблокирован до предоплаты" if wo.awaiting_prepay else ""))
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/payments")
def payment_add(order_id: str, ctx: Ctx = Depends(get_ctx), amount: str = Form(...), method: str = Form("cash"),
                purpose: str = Form("full"), receipt_no: str = Form(""), comment: str = Form("")):
    ctx.need("money.accept")
    wo = _order(ctx, order_id)
    a = msvc.money(_dec(amount))
    if a <= 0 or method not in PAY_METHODS or purpose not in PAY_PURPOSES or wo.status in ("closed", "cancel"):
        flash(ctx.request, "Проверьте сумму и способ оплаты", "error")
        return _to(f"/orders/{order_id}")
    msvc.add_payment(ctx.db, wo, a, method, purpose, receipt_no, comment)
    ctx.db.commit()
    flash(ctx.request, f"Оплата {a} ₴ принята")
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/close")
def order_close(order_id: str, ctx: Ctx = Depends(get_ctx), state: str = Form(""), defects: str = Form(""),
                advice: str = Form(""), debt_comment: str = Form("")):
    ctx.need("money.accept")
    wo = _order(ctx, order_id)
    err = msvc.close(ctx.db, wo, _is_admin(ctx), state, defects, advice, debt_comment)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Автомобиль выдан клиенту, наряд закрыт")
    return _to(f"/orders/{order_id}")


# ======================= Печать =======================
def _print_data(ctx: Ctx, wo: WorkOrder) -> dict:
    db = ctx.db
    return dict(wo=wo, v=db.get(Vehicle, wo.vehicle_id), c=db.get(Client, wo.client_id),
                works=db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all(),
                parts=db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.notin_(PART_OFF))
                                 .order_by(WoPart.created_at)).all(),
                t=msvc.totals(db, wo), manager=db.get(User, wo.manager_id) if wo.manager_id else None,
                payments=db.scalars(select(Payment).where(Payment.work_order_id == wo.id).order_by(Payment.created_at)).all(),
                fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate, PAY_METHODS=PAY_METHODS)


@router.get("/orders/{order_id}/print")
def order_print(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """Заказ-наряд для клиента: только название, кол-во, цена, сумма — без артикулов и закупки."""
    ctx.need("prices.view_sale", "money.view")
    wo = _order(ctx, order_id)
    return ctx.render("money/print_order.html", title=f"Заказ-наряд {wo.number}", **_print_data(ctx, wo))


@router.get("/orders/{order_id}/act/parts")
def act_parts(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """Акт на запчасти (11.11.2026): что поставили на машину — бренд, артикул, кол-во, цена. Из наряда автоматически."""
    ctx.need("prices.view_sale", "money.view")
    wo = _order(ctx, order_id)
    data = _print_data(ctx, wo)
    from ..models import PartCatalog
    cats = {c.id: c for c in ctx.db.scalars(select(PartCatalog).where(
        PartCatalog.id.in_([p.catalog_id for p in data["parts"] if p.catalog_id])))}
    return ctx.render("money/act_parts.html", title=f"Акт запчастей {wo.number}", cats=cats,
                      mechanic=ctx.db.get(User, wo.mechanic_id) if wo.mechanic_id else None, **data)


@router.get("/orders/{order_id}/act/works")
def act_works(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """Акт выполненных работ (11.11.2026): работы, исполнитель, нормо-часы, цена. Из наряда автоматически."""
    ctx.need("prices.view_sale", "money.view")
    wo = _order(ctx, order_id)
    data = _print_data(ctx, wo)
    ids = {w.mechanic_id for w in data["works"] if w.mechanic_id} | ({wo.mechanic_id} if wo.mechanic_id else set())
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}
    done = [w.done_at for w in data["works"] if w.done_at]
    return ctx.render("money/act_works.html", title=f"Акт выполненных работ {wo.number}", people=people,
                      done_at=max(done) if done else None, **data)


@router.get("/orders/{order_id}/act/handover")
def act_handover(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.view_all", "orders.view_own")
    wo = _order(ctx, order_id)
    return ctx.render("money/act_handover.html", title=f"Акт выдачи {wo.number}", **_print_data(ctx, wo))


# ======================= Раздел «Деньги» =======================
@router.get("/money")
def money_view(ctx: Ctx = Depends(get_ctx), day: str = "", p: str = "", d: str = "", f: str = "", t: str = ""):
    """Деньги за период (по умолчанию — сегодня): оплаты, продажи разбора, долги, ждут предоплату."""
    ctx.need("money.view")
    from ..services import periods
    db = ctx.db
    per = periods.parse(p or ("day" if day else ""), d or day, f, t, default="day",
                        allowed=("day", "week", "month", "quarter", "year", "custom"))
    from ..services.attendance import day_bounds
    start, end = day_bounds(per.d1)[0], day_bounds(per.d2)[0] + timedelta(days=1)  # местные дни → UTC
    pays = db.scalars(select(Payment).where(Payment.branch_id == ctx.branch.id, Payment.created_at >= start,
                                            Payment.created_at < end).order_by(Payment.created_at.desc())).all()
    from ..models import UsedSale
    used = db.scalars(select(UsedSale).where(UsedSale.branch_id == ctx.branch.id, UsedSale.status == "sold",
                                             UsedSale.sold_at >= start, UsedSale.sold_at < end)
                      .order_by(UsedSale.sold_at.desc())).all()
    d = per.d1
    by_method = {m: sum((p.amount for p in pays if p.method == m), Decimal(0)) + sum((s.price for s in used if s.pay_method == m), Decimal(0))
                 for m in PAY_METHODS}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([p.work_order_id for p in pays])))}
    # Долги: готовые/оплаченные/закрытые с остатком
    debts = []
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id,
                                                 WorkOrder.status.in_(("ready", "paid", "closed")))):
        t = msvc.totals(db, wo)
        if t.due > 0:
            debts.append((wo, t, db.get(Client, wo.client_id)))
    waiting_prepay = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, WorkOrder.awaiting_prepay.is_(True),
                                                        WorkOrder.status.not_in(("closed", "cancel")))).all()
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    from ..models import PartCatalog, StockItem
    used_names = {}
    for s in used:
        si = db.get(StockItem, s.stock_item_id)
        used_names[s.id] = (si.code, db.get(PartCatalog, si.catalog_id).name, si.id)
    return ctx.render("money/index.html", title="Деньги", d=d, pays=pays, by_method=by_method, orders=orders,
                      used=used, used_names=used_names,
                      total=sum(by_method.values(), Decimal(0)), debts=debts, waiting_prepay=waiting_prepay, people=people,
                      PAY_METHODS=PAY_METHODS, PAY_PURPOSES=PAY_PURPOSES, per=per, one_day=per.d1 == per.d2,
                      bar=periods.Bar(per, "/money", {}))


# ======================= Зарплата =======================
PAY_KINDS = ("week", "month", "quarter", "year", "custom")


def _per(f: str = "", t: str = "", p: str = "", d: str = ""):
    """Период для зарплаты — общая панель дат; старые ссылки f=…&t=… — как «свой период»."""
    from ..services import periods
    return periods.parse(p or ("custom" if f and t else ""), d, f, t, default="month", allowed=PAY_KINDS)


def _period(f: str, t: str, p: str = "", d: str = ""):
    per = _per(f, t, p, d)
    return per.d1, per.d2


def _payroll_target(ctx: Ctx, user: str):
    see_all = ctx.can("payroll.view_all")
    target = ctx.db.get(User, _uuid(user)) if (user and see_all) else (None if see_all else ctx.user)
    if target and target.branch_id != ctx.branch.id:
        raise Forbidden()
    return see_all, target


def _payroll_user_data(ctx: Ctx, target: User, d1: date, d2: date) -> dict:
    db = ctx.db
    rows = psvc.accruals(db, [target.id], d1, d2)
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([a.work_order_id for a in rows if a.work_order_id])))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders.values()])))}
    works = {w.id: w for w in db.scalars(select(WoWork).where(WoWork.id.in_([a.wo_work_id for a in rows if a.wo_work_id])))}
    summ = psvc.summary(db, [target], d1, d2)[target.id]
    return dict(u=target, rows=rows, orders=orders, vehicles=vehicles, works=works, s=summ, d1=d1, d2=d2,
                payouts=psvc.payouts(db, [target.id], d1, d2), COLS=psvc.COLS, column_of=psvc.column_of,
                today_sum=sum((a.amount for a in rows if a.on_date == date.today()), Decimal(0)),
                lines=[psvc.describe(l, {}) for l in (psvc.lines_for(db, target) or psvc.default_lines(db, target))],
                fmt_plate=osvc.fmt_plate, PAY_METHODS=PAY_METHODS)


@router.get("/payroll")
def payroll_view(ctx: Ctx = Depends(get_ctx), user: str = "", f: str = "", t: str = "", p: str = "", d: str = ""):
    ctx.need("payroll.view_own", "payroll.view_all")
    if not (user or f or t or p or d):   # 11.11.2026: главный экран зарплаты — недельные ведомости
        return _to("/payroll/weeks")
    from ..services.periods import Bar
    db = ctx.db
    per = _per(f, t, p, d)
    d1, d2 = per.d1, per.d2
    see_all, target = _payroll_target(ctx, user)
    if target:
        return ctx.render("money/payroll_user.html", title="Зарплата", see_all=see_all, pq=per.qs,
                          bar=Bar(per, "/payroll", {"user": str(target.id) if see_all else ""}),
                          **_payroll_user_data(ctx, target, d1, d2))
    staff = staff_for_period(db, ctx.branch.id, d1)  # уволенные в периоде — тоже, для расчёта
    summ = psvc.summary(db, staff, d1, d2)
    tot = {k: sum((r[k] for r in summ.values()), Decimal(0)) for k in (*psvc.COLS, "accrued", "paid", "due", "hours")}
    return ctx.render("money/payroll_all.html", title="Зарплата", staff=staff, summ=summ, tot=tot, d1=d1, d2=d2,
                      COLS=psvc.COLS, pq=per.qs, bar=Bar(per, "/payroll", {}))


# ======================= Недельная зарплата (11.11.2026) =======================
def _week_start(d: str, own: bool = False) -> date:
    from ..services import payweek as wsvc
    try:
        x = date.fromisoformat(d) if d else None
    except ValueError:
        x = None
    if x is None:
        # Сотрудник видит свою текущую неделю. Бухгалтер и админ в будни — прошлую (её выплачивают),
        # в выходные — текущую (её как раз проверяют)
        x = date.today() if own or date.today().weekday() >= 5 else date.today() - timedelta(days=7)
    return wsvc.monday(x)


def _week_users(ctx: Ctx, start: date):
    if ctx.can("payroll.view_all"):
        return staff_for_period(ctx.db, ctx.branch.id, start)
    return [ctx.user]


@router.get("/payroll/weeks")
def payroll_weeks(ctx: Ctx = Depends(get_ctx), d: str = "", open_user: str = ""):
    ctx.need("payroll.view_own", "payroll.view_all")
    from ..models.payroll import WEEK_STATUS
    from ..services import payweek as wsvc
    db = ctx.db
    start = _week_start(d, own=not ctx.can("payroll.view_all"))
    users = _week_users(ctx, start)
    data = wsvc.week_data(db, ctx.branch.id, start, users)
    all_ids = set()
    for r in data["rows"].values():
        all_ids |= r["orders"]
        all_ids |= {x[1].id for x in r["pending"]}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_(list(all_ids))))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders.values()])))}
    works = {w.id: w for w in db.scalars(select(WoWork).where(WoWork.id.in_(
        [a.wo_work_id for r in data["rows"].values() for a in r["items"] if a.wo_work_id])))}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    hist = wsvc.history(db, ctx.branch.id, users, start, 6) if ctx.can("payroll.view_all") else []
    from ..models import ToolCheckLine
    from ..models.payroll import PERSON_STATUS
    tl_ids = [a.tool_line_id for r in data["rows"].values() for a in r["items"] if a.tool_line_id]
    tool_checks = {l.id: l.check_id for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.id.in_(tl_ids)))} if tl_ids else {}
    return ctx.render("money/payroll_weeks.html", title="Зарплата · неделя", start=start, end=data["end"], data=data,
                      users=users, orders=orders, vehicles=vehicles, works=works, people=people, hist=hist,
                      PERSON_STATUS=PERSON_STATUS, tool_checks=tool_checks,
                      WEEK_STATUS=WEEK_STATUS, COLS=wsvc.COLS, PAY_METHODS=PAY_METHODS, PAY_TO=PAY_TO,
                      prev=(start - timedelta(days=7)).isoformat(), next=(start + timedelta(days=7)).isoformat(),
                      this_week=wsvc.monday(date.today()), open_user=open_user, fmt_plate=osvc.fmt_plate,
                      column_of=psvc.column_of, mask=_mask_account)


@router.get("/payroll/weeks/{d}/sheet")
def payroll_week_sheet(d: str, ctx: Ctx = Depends(get_ctx), user: str = ""):
    """Расчётный лист за неделю: один сотрудник (?user=) или все, у кого есть начисления."""
    ctx.need("payroll.view_own", "payroll.view_all")
    from ..models.payroll import PERSON_STATUS
    from ..services import payweek as wsvc
    db = ctx.db
    see_all = ctx.can("payroll.view_all")
    start = _week_start(d, own=not see_all)
    users = _week_users(ctx, start)
    if user:
        users = [u for u in users if str(u.id) == user]
        if not users:
            raise Forbidden()
    data = wsvc.week_data(db, ctx.branch.id, start, users)
    sheet_users = [u for u in users if data["rows"][u.id]["active"]]
    ids = {g for r in data["rows"].values() for g, _ in r["groups"]}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_(list(ids))))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders.values()])))}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    title = f"Расчётный лист — {sheet_users[0].full_name}" if len(sheet_users) == 1 else "Расчётные листы за неделю"
    return ctx.render("money/payroll_sheet.html", title=title, start=start, end=data["end"], data=data,
                      sheet_users=sheet_users, orders=orders, vehicles=vehicles, people=people, COLS=wsvc.COLS,
                      PERSON_STATUS=PERSON_STATUS, PAY_TO=PAY_TO, mask=_mask_account, fmt_plate=osvc.fmt_plate)


PAY_TO = {"cash": "Наличными", "card": "На карту", "account": "На счёт (IBAN)"}


def _mask_account(v: str | None) -> str:
    v = (v or "").replace(" ", "")
    return f"•••• {v[-4:]}" if len(v) > 4 else v


def _week_back(start: date, uid=None) -> str:
    return f"/payroll/weeks?d={start.isoformat()}" + (f"&open_user={uid}#u-{uid}" if uid else "")


def _week_of(ctx: Ctx, d: str):
    from ..services import payweek as wsvc
    start = _week_start(d)
    return start, wsvc.get_week(ctx.db, ctx.branch.id, start, create=True)


@router.post("/payroll/weeks/{d}/status")
def payroll_week_status(d: str, ctx: Ctx = Depends(get_ctx), status: str = Form(...), note: str = Form("")):
    """Бухгалтер: «Проверил всех» — отметка «проверено» у всех, кого ещё не проверяли.
    Утверждение и выдача — только по каждому сотруднику отдельно (решение 11.11.2026)."""
    from ..services import payweek as wsvc
    if status != "checked":
        raise Forbidden()
    ctx.need("payroll.check")
    start, w = _week_of(ctx, d)
    users = staff_for_period(ctx.db, ctx.branch.id, start)
    data = wsvc.week_data(ctx.db, ctx.branch.id, start, users)
    n = 0
    for uid, r in data["rows"].items():
        if (r["accrued"] > 0 or r["paid"]) and r["pst"] == "open":
            wsvc.set_person(ctx.db, w, uid, "checked", ctx.user)
            n += 1
    w.checked_by, w.checked_at = ctx.user.id, now()
    if note.strip():
        w.note = (w.note + "\n" if w.note else "") + f"{date.today():%d.%m} {ctx.user.short_name}: {note.strip()}"
    wsvc.refresh_paid(ctx.db, w, users)
    ctx.db.commit()
    flash(ctx.request, f"Проверено: {n} чел. — ждут утверждения админа" if n else "Проверять некого — все уже проверены")
    return _to(_week_back(start))


@router.post("/payroll/weeks/{d}/people/{uid}")
def payroll_person_status(d: str, uid: str, ctx: Ctx = Depends(get_ctx), status: str = Form(...)):
    """Один сотрудник: проверено (бухгалтер) → утверждено к выплате (админ / кому дали право) ; вернуть на проверку."""
    from ..services import payweek as wsvc
    need = {"checked": "payroll.check", "approved": "payroll.edit", "open": "payroll.edit"}.get(status)
    if not need:
        raise Forbidden()
    ctx.need(need)
    u = ctx.db.get(User, _uuid(uid))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    start, w = _week_of(ctx, d)
    users = staff_for_period(ctx.db, ctx.branch.id, start)
    wsvc.set_person(ctx.db, w, u.id, status, ctx.user)
    wsvc.refresh_paid(ctx.db, w, users)
    ctx.db.commit()
    flash(ctx.request, {"checked": f"{u.short_name}: проверено — ждёт утверждения", "approved": f"{u.short_name}: утверждено — можно выдавать",
                        "open": f"{u.short_name}: вернули на проверку"}[status])
    return _to(_week_back(start, u.id))


@router.post("/payroll/weeks/{d}/pay")
async def payroll_week_pay(d: str, ctx: Ctx = Depends(get_ctx)):
    """Выдать зарплату за неделю — одному сотруднику (каждый приходит и получает сам). Только утверждённому."""
    from ..services import payweek as wsvc
    ctx.need("payroll.pay")
    start, w = _week_of(ctx, d)
    form = await ctx.request.form()
    users = staff_for_period(ctx.db, ctx.branch.id, start)
    data = wsvc.week_data(ctx.db, ctx.branch.id, start, users)
    targets = [u for u in users if str(u.id) == str(form.get("user_id", ""))]
    if targets and data["rows"][targets[0].id]["pst"] != "approved":
        flash(ctx.request, f"{targets[0].short_name}: сначала бухгалтер проверяет, админ утверждает — тогда выдача", "error")
        return _to(_week_back(start, targets[0].id))
    n, total = 0, Decimal(0)
    for u in targets:
        due = data["rows"][u.id]["due"]
        amount = msvc.money(_dec(form.get("amount"))) if form.get("amount") else due
        if amount <= 0:
            continue
        method = str(form.get("method") or u.pay_method or "cash")
        method = method if method in PAY_TO else "cash"
        wsvc.pay(ctx.db, w, u, amount, method="cash" if method == "cash" else "card",
                 account="" if method == "cash" else f"{PAY_TO[method]} {u.pay_account or ''} {u.pay_bank or ''}".strip(),
                 note=str(form.get("note", "")).strip())
        n, total = n + 1, total + amount
    ctx.db.flush()
    wsvc.refresh_paid(ctx.db, w, users)
    ctx.db.commit()
    flash(ctx.request, f"Выдано: {n} чел. на {msvc.money(total)} ₴" if n else "Выдавать некому — всё уже выдано", "ok" if n else "error")
    return _to(_week_back(start, targets[0].id if len(targets) == 1 else None))


def _edit_guard(ctx: Ctx):
    """Исправлять зарплату — только админ или кому админ дал право «payroll.edit»."""
    ctx.need("payroll.edit")


@router.post("/payroll/weeks/{d}/adjust")
def payroll_week_adjust(d: str, ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), amount: str = Form(...),
                        kind: str = Form("bonus"), note: str = Form(""), on_date: str = Form("")):
    """Премия (+), штраф/удержание (−) или исправление за эту неделю."""
    _edit_guard(ctx)
    from ..services import payweek as wsvc
    start, w = _week_of(ctx, d)
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    a = msvc.money(_dec(amount))
    if a == 0 or not note.strip():
        flash(ctx.request, "Нужны сумма и причина", "error")
        return _to(_week_back(start, u.id))
    a = -abs(a) if kind == "fine" else abs(a)
    try:
        day = date.fromisoformat(on_date) if on_date else start + timedelta(days=6)
    except ValueError:
        day = start + timedelta(days=6)
    day = min(max(day, start), start + timedelta(days=6))
    ctx.db.add(PayrollAccrual(user_id=u.id, base_amount=abs(a), rate=0, amount=a, on_date=day,
                              kind="deduction" if kind == "fine" else "adjust",
                              note=f"{'Штраф' if kind == 'fine' else 'Премия'}: {note.strip()}"[:300]))
    ctx.db.flush()
    wsvc.refresh_paid(ctx.db, w, staff_for_period(ctx.db, ctx.branch.id, start))
    ctx.db.commit()
    flash(ctx.request, f"{'Штраф' if a < 0 else 'Премия'} {a} ₴ — {u.short_name}")
    return _to(_week_back(start, u.id))


@router.post("/payroll/accruals/{aid}")
def payroll_accrual_edit(aid: str, ctx: Ctx = Depends(get_ctx), amount: str = Form(""), reason: str = Form(""),
                         action: str = Form("save"), d: str = Form("")):
    """Исправить сумму начисления или отменить его. Причина обязательна — видна в строке и в журнале."""
    _edit_guard(ctx)
    from ..services import payweek as wsvc
    a = ctx.db.get(PayrollAccrual, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id:
        raise Forbidden()
    start = wsvc.monday(a.on_date)
    if not reason.strip():
        flash(ctx.request, "Напишите причину исправления", "error")
        return _to(_week_back(start, a.user_id))
    if action == "cancel":
        a.status = "cancelled"
        a.note = f"{a.note} · отменено: {reason.strip()}"[:300]
        msg = "Начисление отменено"
    else:
        new = msvc.money(_dec(amount))
        a.note = f"{a.note} · было {a.amount} ₴: {reason.strip()}"[:300]
        a.amount = new
        msg = f"Исправлено: {new} ₴"
    ctx.db.flush()
    w = wsvc.get_week(ctx.db, ctx.branch.id, start)
    if w:
        wsvc.refresh_paid(ctx.db, w, staff_for_period(ctx.db, ctx.branch.id, start))
    ctx.db.commit()
    flash(ctx.request, msg)
    return _to(_week_back(start, a.user_id))


@router.post("/payroll/weeks/{d}/salary")
def payroll_week_salary(d: str, ctx: Ctx = Depends(get_ctx)):
    """Начислить дневной оклад за неделю по табелю (кто был на месте)."""
    _edit_guard(ctx)
    from ..services import payweek as wsvc
    start, w = _week_of(ctx, d)
    total = wsvc.accrue_week_salary(ctx.db, ctx.branch.id, start, staff_for_period(ctx.db, ctx.branch.id, start))
    ctx.db.commit()
    flash(ctx.request, f"Оклад за неделю начислен: {msvc.money(total)} ₴" if total else "Начислять нечего: нет дневных окладов или уже начислено")
    return _to(_week_back(start))


@router.post("/payroll/account/{uid}")
def payroll_account(uid: str, ctx: Ctx = Depends(get_ctx), pay_method: str = Form("cash"), pay_account: str = Form(""),
                    pay_bank: str = Form(""), back: str = Form("")):
    """Куда платить сотруднику: наличными / на карту / на счёт, номер карты или IBAN, банк."""
    _edit_guard(ctx)
    u = ctx.db.get(User, _uuid(uid))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    u.pay_method = pay_method if pay_method in PAY_TO else "cash"
    u.pay_account = "".join(ch for ch in pay_account.upper() if ch.isalnum())[:64]
    u.pay_bank = pay_bank.strip()[:60]
    ctx.db.commit()
    flash(ctx.request, f"Реквизиты {u.short_name} сохранены")
    return _to(back if back.startswith("/") and not back.startswith("//") else "/payroll/weeks")


@router.get("/payroll/print")
def payroll_print(ctx: Ctx = Depends(get_ctx), user: str = "", f: str = "", t: str = "", p: str = "", d: str = ""):
    """Расчётный лист сотрудника или ведомость на всех — для бумажной папки."""
    ctx.need("payroll.view_own", "payroll.view_all")
    d1, d2 = _period(f, t, p, d)
    see_all, target = _payroll_target(ctx, user)
    if target:
        return ctx.render("money/payroll_print_user.html", title=f"Расчётный лист — {target.full_name}",
                          **_payroll_user_data(ctx, target, d1, d2))
    staff = staff_for_period(ctx.db, ctx.branch.id, d1)
    summ = psvc.summary(ctx.db, staff, d1, d2)
    tot = {k: sum((r[k] for r in summ.values()), Decimal(0)) for k in (*psvc.COLS, "accrued", "paid", "due", "hours")}
    return ctx.render("money/payroll_print_all.html", title="Ведомость по зарплате", staff=staff, summ=summ, tot=tot,
                      d1=d1, d2=d2, COLS=psvc.COLS)


@router.get("/payroll/export.csv")
def payroll_csv(ctx: Ctx = Depends(get_ctx), f: str = "", t: str = "", p: str = "", d: str = ""):
    ctx.need("payroll.view_all")
    import csv
    import io
    if p:
        f, t = (x.isoformat() for x in _period(f, t, p, d))
    from fastapi.responses import Response
    d1, d2 = _period(f, t)
    staff = staff_for_period(ctx.db, ctx.branch.id, d1)
    summ = psvc.summary(ctx.db, staff, d1, d2)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Сотрудник", "Должность", "Нарядов", "Нормо-часы", *psvc.COLS.values(), "Начислено", "Выплачено", "К выплате"])
    for u in staff:
        r = summ[u.id]
        w.writerow([u.full_name, u.role.name, r["n_orders"], str(r["hours"]).replace(".", ","),
                    *[str(r[k]).replace(".", ",") for k in psvc.COLS], *[str(r[k]).replace(".", ",") for k in ("accrued", "paid", "due")]])
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="zarplata_{d1}_{d2}.csv"'})


@router.post("/payroll/payouts")
def payroll_payout(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), amount: str = Form(...), method: str = Form("cash"),
                   note: str = Form(""), f: str = Form(""), t: str = Form("")):
    """Выплата зарплаты (аванс / расчёт) — чтобы видеть «к выплате»."""
    ctx.need("payroll.manage")
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    d1, d2 = _period(f, t)
    a = msvc.money(_dec(amount))
    back = f"/payroll?user={u.id}&f={d1}&t={d2}"
    if a <= 0:
        flash(ctx.request, "Сумма выплаты должна быть больше нуля", "error")
        return _to(back)
    from ..models import PayrollPayout
    ctx.db.add(PayrollPayout(user_id=u.id, period_from=d1, period_to=d2, amount=a, paid_on=date.today(),
                             method=method if method in PAY_METHODS else "cash", note=note.strip()[:300]))
    ctx.db.commit()
    flash(ctx.request, f"Выплата {a} ₴ записана")
    return _to(back)


@router.post("/payroll/payouts/{pid}/delete")
def payroll_payout_delete(pid: str, ctx: Ctx = Depends(get_ctx)):
    """Удалить ошибочную выплату — только админ или кому дано право исправлять зарплату."""
    ctx.need("payroll.edit")
    from ..models import PayrollPayout, PayWeek
    p = ctx.db.get(PayrollPayout, _uuid(pid))
    if not p or p.branch_id != ctx.branch.id:
        raise Forbidden()
    uid, wid = p.user_id, p.week_id
    ctx.db.delete(p)
    ctx.db.flush()
    if wid:
        from ..services import payweek as wsvc
        w = ctx.db.get(PayWeek, wid)
        wsvc.refresh_paid(ctx.db, w, staff_for_period(ctx.db, ctx.branch.id, w.week_start))
        ctx.db.commit()
        flash(ctx.request, "Выплата удалена — сумма снова «к выдаче»")
        return _to(_week_back(w.week_start, uid))
    ctx.db.commit()
    flash(ctx.request, "Выплата удалена")
    return _to(f"/payroll?user={uid}")


@router.get("/settings/payroll")
def payroll_settings(ctx: Ctx = Depends(get_ctx), role: str = ""):
    """Схемы и оклад (11.11.2026): сначала плитки должностей, внутри — сотрудники этой должности."""
    ctx.need("settings.access")
    db = ctx.db
    everyone = db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True)).order_by(User.full_name)).all()
    roles = {}
    for u in everyone:
        roles.setdefault(u.role.code, {"role": u.role, "n": 0, "salary": 0})["n"] += 1
    staff = [u for u in everyone if u.role.code == role] if role in roles else []
    groups = db.scalars(select(PartGroup).where(PartGroup.branch_id == ctx.branch.id).order_by(PartGroup.name)).all()
    gnames = {g.id: g.name for g in groups}
    group_items = {g.id: db.scalars(select(PartCatalog).join(PartGroupItem, PartGroupItem.catalog_id == PartCatalog.id)
                                    .where(PartGroupItem.group_id == g.id).order_by(PartCatalog.name)).all() for g in groups}
    schemes = {}
    for u in everyone:
        own = psvc.lines_for(db, u)
        if any(l.kind == "salary" for l in own):
            roles[u.role.code]["salary"] += 1
        if u in staff:
            schemes[u.id] = [(l, psvc.describe(l, gnames)) for l in own]
    from ..services.attendance import days_present
    t = date.today()
    present = {u.id: days_present(db, ctx.branch.id, u.id, t.year, t.month) for u in staff}
    order = ["mechanic", "partsman", "manager", "storekeeper", "content", "accountant", "director", "admin"]
    role_list = sorted(roles.values(), key=lambda x: (order.index(x["role"].code) if x["role"].code in order else 99, x["role"].name))
    return ctx.render("money/payroll_settings.html", title="Зарплата: схемы", staff=staff, schemes=schemes, groups=groups,
                      present=present, role_list=role_list, role=role if role in roles else "",
                      role_name=roles[role]["role"].name if role in roles else "",
                      group_items=group_items, defaults=st.get(db, ctx.branch.id, "payroll"), is_admin=_is_admin(ctx),
                      KINDS=psvc.KINDS, BASES=psvc.BASES, SOURCES=psvc.SOURCES, PERIODS=psvc.PERIODS,
                      default_desc={u.id: [psvc.describe(l, gnames) for l in psvc.default_lines(db, u)] for u in staff})


def _admin_only(ctx: Ctx):
    if not _is_admin(ctx):
        raise Forbidden()


@router.post("/settings/payroll/defaults")
def payroll_defaults(ctx: Ctx = Depends(get_ctx), mechanic_works_pct: str = Form("30"), partsman_margin_pct: str = Form("7")):
    _admin_only(ctx)
    st.put(ctx.db, ctx.branch.id, "payroll", {
        "mechanic_works_pct": float(max(Decimal(0), min(Decimal(100), _dec(mechanic_works_pct)))),
        "partsman_margin_pct": float(max(Decimal(0), min(Decimal(30), _dec(partsman_margin_pct))))})
    ctx.db.commit()
    flash(ctx.request, "Проценты по умолчанию сохранены")
    return _to("/settings/payroll")


@router.post("/settings/payroll/{user_id}/lines")
def payroll_line_add(user_id: str, ctx: Ctx = Depends(get_ctx), kind: str = Form("percent"), base: str = Form("works"),
                     rate: str = Form("0"), salary_period: str = Form("day"), filter_source: str = Form(""),
                     filter_group_id: str = Form("")):
    _admin_only(ctx)
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id or kind not in psvc.KINDS:
        raise Forbidden()
    r = _dec(rate)
    if kind == "percent" and base == "parts_margin" and u.role.code == "partsman":
        r = min(r, Decimal(30))  # запчастисту — не больше 30 % от наценки
    s = psvc.user_scheme(ctx.db, u, create=True)
    g = ctx.db.get(PartGroup, uuid.UUID(filter_group_id)) if filter_group_id else None
    ctx.db.add(PaySchemeLine(scheme_id=s.id, kind=kind, base=base if kind != "salary" else None,
                             salary_period=salary_period if kind == "salary" else None, rate=max(r, Decimal(0)),
                             filter_source=filter_source if filter_source in psvc.SOURCES and filter_source else None,
                             filter_group_id=g.id if g and g.branch_id == ctx.branch.id else None,
                             priority=0))
    ctx.db.commit()
    return _to(f"/settings/payroll?role={u.role.code}#u-{user_id}")


@router.post("/settings/payroll/lines/{line_id}")
def payroll_line_edit(line_id: str, ctx: Ctx = Depends(get_ctx), rate: str = Form(...), role: str = Form("")):
    """Исправить сумму оклада (или процент) в строке схемы, не удаляя её. Пишется в журнал."""
    _admin_only(ctx)
    l = ctx.db.get(PaySchemeLine, _uuid(line_id))
    if not l or l.branch_id != ctx.branch.id:
        raise Forbidden()
    r = max(_dec(rate), Decimal(0))
    if l.kind == "percent" and l.base == "parts_margin":
        r = min(r, Decimal(30))   # запчастисту — не больше 30 % от наценки
    old = l.rate
    l.rate = r
    # Уже начислено по этой строке, но ещё не выдано — пересчитываем по новой ставке.
    # Если за ту неделю сотруднику уже выдали деньги — не трогаем (исправить вручную: «Зарплата» → «Исправить»).
    from ..models import PayrollPayout
    from ..services import payweek as wsvc
    from ..web import num
    fixed, paid_weeks = 0, set()
    if old != r:
        for a in ctx.db.scalars(select(PayrollAccrual).where(PayrollAccrual.scheme_line_id == l.id,
                                                             PayrollAccrual.status != "cancelled")):
            wk = wsvc.get_week(ctx.db, ctx.branch.id, wsvc.monday(a.on_date))
            if wk and ctx.db.scalar(select(PayrollPayout.id).where(PayrollPayout.week_id == wk.id,
                                                                   PayrollPayout.user_id == a.user_id)):
                paid_weeks.add(wsvc.monday(a.on_date))
                continue
            if l.kind == "percent":
                new = msvc.money(a.base_amount * r / 100)
            elif l.kind == "salary":
                new = msvc.money(a.base_amount * r)            # база — дни (или месяцы)
            elif old:
                new = msvc.money(a.amount * r / old)           # «за штуку»: та же доля, что и у ставки
            else:
                continue
            if new != a.amount:
                a.note = f"{a.note} · ставка исправлена: было {num(old)}"[:300]
                a.amount, a.rate = new, r
                fixed += 1
    ctx.db.commit()
    msg = f"Сохранено: было {num(old)}, стало {num(r)}"
    if fixed:
        msg += f". Пересчитано начислений: {fixed}"
    if paid_weeks:
        msg += (". За недели " + ", ".join(d.strftime("%d.%m") for d in sorted(paid_weeks)) +
                " деньги уже выданы — там исправьте в «Зарплате» кнопкой «Исправить»")
    flash(ctx.request, msg)
    return _to("/settings/payroll" + (f"?role={role}" if role.isidentifier() else ""))


@router.post("/settings/payroll/lines/{line_id}/delete")
def payroll_line_delete(line_id: str, ctx: Ctx = Depends(get_ctx), role: str = Form("")):
    _admin_only(ctx)
    l = ctx.db.get(PaySchemeLine, _uuid(line_id))
    if l and l.branch_id == ctx.branch.id:
        # По строке уже начисляли — начисления остаются как есть (сумма и подпись), только отвязываем от строки
        for a in ctx.db.scalars(select(PayrollAccrual).where(PayrollAccrual.scheme_line_id == l.id)):
            a.scheme_line_id = None
        ctx.db.flush()
        ctx.db.delete(l)
        ctx.db.commit()
        flash(ctx.request, "Строка схемы убрана. Уже начисленное остаётся в зарплате.")
    return _to("/settings/payroll" + (f"?role={role}" if role.isidentifier() else ""))


@router.post("/settings/payroll/groups")
def group_add(ctx: Ctx = Depends(get_ctx), name: str = Form(...)):
    _admin_only(ctx)
    if name.strip():
        ctx.db.add(PartGroup(name=name.strip()[:120]))
        ctx.db.commit()
    return _to("/settings/payroll#groups")


@router.post("/settings/payroll/groups/{group_id}/items")
def group_item(group_id: str, ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(...), remove: str = Form("")):
    _admin_only(ctx)
    g = ctx.db.get(PartGroup, _uuid(group_id))
    c = ctx.db.get(PartCatalog, _uuid(catalog_id))
    if not g or not c or g.branch_id != ctx.branch.id:
        raise Forbidden()
    row = ctx.db.scalar(select(PartGroupItem).where(PartGroupItem.group_id == g.id, PartGroupItem.catalog_id == c.id))
    if remove and row:
        ctx.db.delete(row)
    elif not remove and not row:
        ctx.db.add(PartGroupItem(group_id=g.id, catalog_id=c.id))
    ctx.db.commit()
    return _to("/settings/payroll#groups")


@router.post("/settings/payroll/salary")
async def salary_accrue(ctx: Ctx = Depends(get_ctx)):
    """Оклад за период: по каждому сотруднику — сколько дней отработал (для дневной ставки) и/или месяц."""
    _admin_only(ctx)
    form = await ctx.request.form()
    note = str(form.get("note", "")).strip() or f"Оклад за {date.today():%m.%Y}"
    total = Decimal(0)
    for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))):
        days = int(_dec(form.get(f"days_{u.id}", "0")))
        months = 1 if form.get(f"month_{u.id}") else 0
        total += psvc.accrue_salary(ctx.db, u, max(days, 0), months, note)
    ctx.db.commit()
    flash(ctx.request, f"Оклад начислен: {msvc.money(total)} ₴")
    role = str(form.get("role", ""))
    return _to("/settings/payroll" + (f"?role={role}" if role.isidentifier() else ""))
