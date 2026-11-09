"""Бумажные «папки» (пункт 4): всё по клиенту и всё по сотруднику на A4 — на случай, если CRM недоступна."""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..models import (Client, ClientEvent, Lift, Payment, Tool, User, Vehicle, WoFinding, WoPart, WorkOrder,
                      WoWork)
from ..models.money import PAY_METHODS
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, URGENCY, WO_STATUSES
from ..services import money as msvc
from ..services import orders as osvc
from ..services import payroll as psvc
from ..web import Ctx, Forbidden, get_ctx
from .orders import _uuid

router = APIRouter()


@router.get("/clients/{client_id}/print")
def client_folder(client_id: str, ctx: Ctx = Depends(get_ctx)):
    """Папка клиента: карточка, авто, все наряды с работами/запчастями/оплатами, рекомендации."""
    ctx.need("clients.view")
    db = ctx.db
    c = db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    vehicles = db.scalars(select(Vehicle).where(Vehicle.client_id == c.id).order_by(Vehicle.created_at)).all()
    orders = db.scalars(osvc.visible_orders_query(ctx).where(WorkOrder.client_id == c.id)
                        .order_by(WorkOrder.created_at.desc())).all()
    show_money = ctx.can("prices.view_sale", "money.view")
    docs = []
    for wo in orders:
        docs.append({
            "wo": wo, "t": msvc.totals(db, wo),
            "works": db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all(),
            "parts": db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status != "returned")
                                .order_by(WoPart.created_at)).all(),
            "findings": db.scalars(select(WoFinding).where(WoFinding.work_order_id == wo.id)
                                   .order_by(WoFinding.created_at)).all(),
            "payments": db.scalars(select(Payment).where(Payment.work_order_id == wo.id)
                                   .order_by(Payment.created_at)).all(),
        })
    recs = db.scalars(select(ClientEvent).where(ClientEvent.client_id == c.id, ClientEvent.kind == "recommendation",
                                                ClientEvent.resolved.is_(False)).order_by(ClientEvent.created_at.desc())).all()
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("folders/client.html", title=f"Папка клиента — {c.full_name}", c=c, vehicles=vehicles,
                      vmap={v.id: v for v in vehicles}, docs=docs, recs=recs, people=people, show_money=show_money,
                      STATUSES=WO_STATUSES, URGENCY=URGENCY, INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
                      PAY_METHODS=PAY_METHODS, fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate)


@router.get("/staff/{user_id}/print")
def staff_folder(user_id: str, ctx: Ctx = Depends(get_ctx), f: str = "", t: str = ""):
    """Папка сотрудника: карточка, пост, наряды и выполненные работы, табель, зарплата, инструмент на руках."""
    db = ctx.db
    u = db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    if u.id != ctx.user.id and not ctx.can("users.manage", "payroll.view_all", "attendance.view_all"):
        raise Forbidden()
    from .money import _period
    d1, d2 = _period(f, t)
    lift = db.scalar(select(Lift).where(Lift.mechanic_id == u.id))
    from datetime import datetime, time, timezone
    t1 = datetime.combine(d1, time.min).astimezone(timezone.utc)
    t2 = datetime.combine(d2, time.max).astimezone(timezone.utc)
    works = db.scalars(select(WoWork).where(WoWork.mechanic_id == u.id, WoWork.status == "done",
                                            WoWork.done_at >= t1, WoWork.done_at <= t2).order_by(WoWork.done_at)).all()
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([w.work_order_id for w in works])))}
    from ..models.orders import WO_ACTIVE
    from sqlalchemy import or_
    active = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, WorkOrder.status.in_(WO_ACTIVE),
                                                or_(WorkOrder.mechanic_id == u.id, WorkOrder.partsman_id == u.id,
                                                    WorkOrder.manager_id == u.id))).all()
    vids = [o.vehicle_id for o in [*orders.values(), *active]]
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_(vids)))}
    # Табель за месяцы периода
    from ..services.attendance import month_grid
    days = []
    y, m = d1.year, d1.month
    while (y, m) <= (d2.year, d2.month):
        for dn, d in sorted(month_grid(db, ctx.branch.id, [u], y, m)[u.id].items()):
            dd = date(y, m, dn)
            if d1 <= dd <= d2 and (d.minutes or d.open):
                days.append((dd, d))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    can_pay = u.id == ctx.user.id and ctx.can("payroll.view_own") or ctx.can("payroll.view_all")
    pay = psvc.summary(db, [u], d1, d2)[u.id] if can_pay else None
    tools = db.scalars(select(Tool).where(Tool.holder_id == u.id).order_by(Tool.inv_no)).all() \
        if (u.id == ctx.user.id or ctx.can("tools.view")) else []
    return ctx.render("folders/staff.html", title=f"Папка сотрудника — {u.full_name}", u=u, lift=lift, works=works,
                      orders=orders, active=active, vehicles=vehicles, days=days, pay=pay, tools=tools, d1=d1, d2=d2,
                      COLS=psvc.COLS, STATUSES=WO_STATUSES, fmt_plate=osvc.fmt_plate,
                      hours_total=sum((Decimal(w.hours or 0) * Decimal(w.qty or 1) for w in works), Decimal(0)),
                      minutes_total=sum(d.minutes for _, d in days))
