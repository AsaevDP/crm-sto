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
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid

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
    if not w or w.work_order_id != wo.id or wo.status != "work":
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
        w.status, w.done_at = "done", t
        db.flush()
        left = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.status != "done")).all()
        if not left:
            osvc.set_status(db, wo, "ready", "Все работы выполнены")
            msvc.mark_paid_if_settled(db, wo)
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
                parts=db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status != "returned")
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


@router.get("/orders/{order_id}/act/handover")
def act_handover(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.view_all", "orders.view_own")
    wo = _order(ctx, order_id)
    return ctx.render("money/act_handover.html", title=f"Акт выдачи {wo.number}", **_print_data(ctx, wo))


# ======================= Раздел «Деньги» =======================
@router.get("/money")
def money_view(ctx: Ctx = Depends(get_ctx), day: str = ""):
    ctx.need("money.view")
    db = ctx.db
    try:
        d = date.fromisoformat(day) if day else date.today()
    except ValueError:
        d = date.today()
    start = datetime.combine(d, datetime.min.time()).astimezone()
    pays = db.scalars(select(Payment).where(Payment.branch_id == ctx.branch.id, Payment.created_at >= start,
                                            Payment.created_at < start + timedelta(days=1)).order_by(Payment.created_at.desc())).all()
    from ..models import UsedSale
    used = db.scalars(select(UsedSale).where(UsedSale.branch_id == ctx.branch.id, UsedSale.status == "sold",
                                             UsedSale.sold_at >= start, UsedSale.sold_at < start + timedelta(days=1))
                      .order_by(UsedSale.sold_at.desc())).all()
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
                      PAY_METHODS=PAY_METHODS, PAY_PURPOSES=PAY_PURPOSES, prev=(d - timedelta(days=1)).isoformat(),
                      next=(d + timedelta(days=1)).isoformat())


# ======================= Зарплата =======================
def _period(f: str, t: str):
    today = date.today()
    try:
        d1 = date.fromisoformat(f) if f else today.replace(day=1)
        d2 = date.fromisoformat(t) if t else today
    except ValueError:
        d1, d2 = today.replace(day=1), today
    return d1, d2


@router.get("/payroll")
def payroll_view(ctx: Ctx = Depends(get_ctx), user: str = "", f: str = "", t: str = ""):
    ctx.need("payroll.view_own", "payroll.view_all")
    db = ctx.db
    d1, d2 = _period(f, t)
    see_all = ctx.can("payroll.view_all")
    target = db.get(User, _uuid(user)) if (user and see_all) else (None if see_all else ctx.user)
    staff = db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True)).order_by(User.full_name)).all()
    if target:
        rows = psvc.accruals(db, [target.id], d1, d2)
        orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([a.work_order_id for a in rows if a.work_order_id])))}
        days = {}
        for a in rows:
            days.setdefault(a.on_date, []).append(a)
        today_sum = sum((a.amount for a in rows if a.on_date == date.today()), Decimal(0))
        return ctx.render("money/payroll_user.html", title="Зарплата", u=target, days=days, orders=orders,
                          total=sum((a.amount for a in rows), Decimal(0)), today_sum=today_sum, d1=d1, d2=d2,
                          see_all=see_all, lines=[psvc.describe(l, {}) for l in (psvc.lines_for(db, target) or psvc.default_lines(db, target))])
    rows = psvc.accruals(db, [u.id for u in staff], d1, d2)
    sums = {}
    for a in rows:
        sums[a.user_id] = sums.get(a.user_id, Decimal(0)) + a.amount
    return ctx.render("money/payroll_all.html", title="Зарплата", staff=staff, sums=sums, d1=d1, d2=d2,
                      total=sum(sums.values(), Decimal(0)))


@router.get("/settings/payroll")
def payroll_settings(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    db = ctx.db
    staff = db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True)).order_by(User.full_name)).all()
    groups = db.scalars(select(PartGroup).where(PartGroup.branch_id == ctx.branch.id).order_by(PartGroup.name)).all()
    gnames = {g.id: g.name for g in groups}
    group_items = {g.id: db.scalars(select(PartCatalog).join(PartGroupItem, PartGroupItem.catalog_id == PartCatalog.id)
                                    .where(PartGroupItem.group_id == g.id).order_by(PartCatalog.name)).all() for g in groups}
    schemes = {}
    for u in staff:
        own = psvc.lines_for(db, u)
        schemes[u.id] = [(l, psvc.describe(l, gnames)) for l in own]
    return ctx.render("money/payroll_settings.html", title="Зарплата: схемы", staff=staff, schemes=schemes, groups=groups,
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
    return _to("/settings/payroll#u-" + user_id)


@router.post("/settings/payroll/lines/{line_id}/delete")
def payroll_line_delete(line_id: str, ctx: Ctx = Depends(get_ctx)):
    _admin_only(ctx)
    l = ctx.db.get(PaySchemeLine, _uuid(line_id))
    if l and l.branch_id == ctx.branch.id:
        ctx.db.delete(l)
        ctx.db.commit()
    return _to("/settings/payroll")


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
    return _to("/settings/payroll")
