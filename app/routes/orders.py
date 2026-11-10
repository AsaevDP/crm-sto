"""Наряды: список, приёмка (новый наряд), карточка, фото, клиенты."""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select

from ..models import Client, ClientEvent, Lift, User, Vehicle, WoFinding, WoPhoto, WorkOrder, WoStatusHistory, WoWork
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, URGENCY, WO_ACTIVE, WO_STATUSES
from ..services import orders as svc
from ..web import Ctx, Forbidden, flash, get_ctx

router = APIRouter()


def _uuid(v: str):
    try:
        return uuid.UUID(v)
    except (ValueError, TypeError):
        raise Forbidden()


def _order(ctx: Ctx, order_id: str) -> WorkOrder:
    wo = ctx.db.get(WorkOrder, _uuid(order_id))
    if not wo or not svc.can_view(ctx, wo):
        raise Forbidden()
    return wo


def _people(ctx: Ctx, *ids) -> dict:
    ids = [i for i in ids if i]
    return {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _cards(ctx: Ctx, orders: list[WorkOrder]) -> list[dict]:
    """Данные для карточек списка: авто, клиент, слесарь, подъёмник, первое фото."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))}
    clients = {c.id: c for c in ctx.db.scalars(select(Client).where(Client.id.in_([o.client_id for o in orders])))}
    lifts = {l.id: l for l in ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id))}
    people = _people(ctx, *[o.mechanic_id for o in orders], *[o.partsman_id for o in orders])
    first_photo = {}
    for p in ctx.db.scalars(select(WoPhoto).where(WoPhoto.work_order_id.in_(ids)).order_by(WoPhoto.created_at)):
        first_photo.setdefault(p.work_order_id, p)
    n_photos = dict(ctx.db.execute(select(WoPhoto.work_order_id, func.count()).where(WoPhoto.work_order_id.in_(ids))
                                   .group_by(WoPhoto.work_order_id)).all())
    prog = svc.work_progress(ctx.db, ids)
    from ..models import WoPart
    urgent = set(ctx.db.scalars(select(WoPart.work_order_id).where(   # слесарь просит срочно — видно на карточке
        WoPart.work_order_id.in_(ids), WoPart.urgent.is_(True), WoPart.status.in_(("need", "ordered", "arrived", "reserved")))))
    return [{
        "urgent": o.id in urgent,
        "o": o, "v": vehicles.get(o.vehicle_id), "prog": prog.get(o.id), "c": clients.get(o.client_id), "lift": lifts.get(o.lift_id),
        "mechanic": people.get(o.mechanic_id), "partsman": people.get(o.partsman_id),
        "photo": first_photo.get(o.id), "n_photos": n_photos.get(o.id, 0),
    } for o in orders]


# ---------- Список ----------
@router.get("/orders")
def orders_list(ctx: Ctx = Depends(get_ctx), tab: str = "active", q: str = "", status: str = "",
                p: str = "", d: str = "", f: str = "", t: str = ""):
    ctx.need("orders.view_all", "orders.view_own")
    from ..services import periods
    from ..services.attendance import day_bounds
    per = periods.parse(p, d, f, t, default="all", allowed=("day", "week", "month", "quarter", "year", "custom", "all"))
    query = svc.visible_orders_query(ctx)
    if per.kind != "all":   # принятые за период
        from datetime import timedelta
        query = query.where(WorkOrder.created_at >= day_bounds(per.d1)[0],
                            WorkOrder.created_at < day_bounds(per.d2)[0] + timedelta(days=1))
    if status in WO_STATUSES:
        query = query.where(WorkOrder.status == status)
    elif tab == "active":
        query = query.where(WorkOrder.status.in_(WO_ACTIVE))
    q = q.strip()
    if q:
        plate, digits = svc.norm_plate(q), "".join(ch for ch in q if ch.isdigit())
        conds = [WorkOrder.number.contains(q), Vehicle.plate.contains(plate), Client.full_name.ilike(f"%{q}%")]
        if len(digits) >= 3:
            conds.append(Client.phone.contains(digits))
        query = (query.join(Vehicle, Vehicle.id == WorkOrder.vehicle_id).join(Client, Client.id == WorkOrder.client_id)
                 .where(or_(*conds)))
    orders = ctx.db.scalars(query.order_by(WorkOrder.created_at.desc()).limit(200)).all()
    lifts = []
    if ctx.can("lifts.take") and ctx.user.role.code == "mechanic":
        lifts = ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id, Lift.active.is_(True))
                               .order_by(Lift.number)).all()
    return ctx.render("orders/list.html", title="Наряды", cards=_cards(ctx, orders), tab=tab, q=q,
                      STATUSES=WO_STATUSES, fmt_plate=svc.fmt_plate, status=status, my_lifts=lifts,
                      lift_people=_people(ctx, *[l.mechanic_id for l in lifts]), per=per,
                      bar=periods.Bar(per, "/orders", {"tab": tab, "q": q, "status": status}))


@router.post("/lifts/take")
def lift_take(ctx: Ctx = Depends(get_ctx), lift_id: str = Form("")):
    """Слесарь сам закрепляется за подъёмником (или открепляется — пустое значение).
    Занятый другим слесарем пост забрать нельзя — это решает админ в настройках."""
    ctx.need("lifts.take")
    if ctx.user.role.code != "mechanic":
        raise Forbidden()
    lift = ctx.db.get(Lift, _uuid(lift_id)) if lift_id else None
    if lift and (lift.branch_id != ctx.branch.id or not lift.active):
        raise Forbidden()
    if lift and lift.mechanic_id and lift.mechanic_id != ctx.user.id:
        flash(ctx.request, "Этот подъёмник закреплён за другим слесарем", "error")
        return RedirectResponse("/orders", status_code=303)
    for l in ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id, Lift.mechanic_id == ctx.user.id)):
        if not lift or l.id != lift.id:
            l.mechanic_id = None
    if lift:
        lift.mechanic_id = ctx.user.id
    ctx.db.commit()
    flash(ctx.request, f"Вы закреплены за подъёмником {lift.number}" if lift else "Вы откреплены от подъёмника")
    return RedirectResponse("/orders", status_code=303)


# ---------- Приёмка ----------
@router.get("/orders/new")
def order_new(ctx: Ctx = Depends(get_ctx), appt: str = ""):
    ctx.need("orders.create")
    bid = ctx.branch.id
    from .schedule import prefill_from
    prefill = prefill_from(ctx, appt) if appt else {}
    partsmen = svc.staff_by_role(ctx.db, bid, "partsman")
    return ctx.render("orders/new.html", title="Приёмка",
                      partsmen=partsmen, my_partsman=svc.paired_partsman(ctx.db, ctx.user.id, partsmen),
                      mechanics=svc.staff_by_role(ctx.db, bid, "mechanic"),
                      lifts=svc.lifts_with_load(ctx.db, bid), makes=svc.known_makes(ctx.db, bid),
                      INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS, MAX_PHOTOS=svc.MAX_PHOTOS,
                      fmt_plate=svc.fmt_plate, prefill=prefill)


@router.post("/orders/new")
async def order_create(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Форма приёмки отправляется из JS (FormData): поля + сжатые на телефоне фото и миниатюры."""
    ctx.need("orders.create")
    form = await request.form(max_files=2 * svc.MAX_PHOTOS + 2)
    f = svc.IntakeForm(**{k: str(form.get(k, "")) for k in svc.IntakeForm.__dataclass_fields__ if k != "intake_items"},
                       intake_items=[str(x) for x in form.getlist("intake_items")])
    photos = [p for p in form.getlist("photos") if hasattr(p, "read")][:svc.MAX_PHOTOS]
    thumbs = [p for p in form.getlist("thumbs") if hasattr(p, "read")]
    errors = f.errors(len(photos))
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    wo = svc.create_order(ctx.db, ctx, f)
    saved = 0
    for i, p in enumerate(photos):
        thumb = await thumbs[i].read() if i < len(thumbs) else None
        if svc.save_photo(ctx.db, wo, await p.read(), thumb):
            saved += 1
    if not saved:
        ctx.db.rollback()
        return JSONResponse({"ok": False, "errors": ["Фото не распознаны (нужны JPG/PNG)"]}, status_code=400)
    svc.route_after_intake(ctx.db, wo)
    if form.get("appt_id"):
        from .schedule import link_order
        link_order(ctx, str(form.get("appt_id")), wo)
    ctx.db.commit()
    flash(request, f"Наряд {wo.number} создан")
    return {"ok": True, "url": f"/orders/{wo.id}"}


@router.get("/api/clients/search")
def clients_search(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("orders.create", "clients.view")
    return {"items": svc.search_clients(ctx.db, ctx.branch.id, q)}


# ---------- Карточка наряда ----------
@router.get("/orders/{order_id}")
def order_view(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.view_all", "orders.view_own")
    wo = _order(ctx, order_id)
    db = ctx.db
    photos = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id).order_by(WoPhoto.created_at)).all()
    works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all()
    history = db.scalars(select(WoStatusHistory).where(WoStatusHistory.work_order_id == wo.id)
                         .order_by(WoStatusHistory.created_at)).all()
    findings = db.scalars(select(WoFinding).where(WoFinding.work_order_id == wo.id).order_by(WoFinding.created_at)).all()
    people = _people(ctx, wo.manager_id, wo.partsman_id, wo.mechanic_id, *[h.created_by for h in history],
                     *[f.created_by for f in findings], *[w.mechanic_id for w in works],
                     *[w.done_by for w in works])
    can_assign = ctx.can("orders.assign") and wo.status in WO_ACTIVE
    is_mech = ctx.user.id == wo.mechanic_id or ctx.can("orders.view_all")
    # Запчасти наряда (артикул/закупка/наценка — только запчастисту и админу)
    from ..models import IssueRequest, PartCatalog, WoPart
    from ..services import settings as st_
    from ..services import stock as stock_svc
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id).order_by(WoPart.created_at)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([p.catalog_id for p in parts if p.catalog_id])))}
    people.update(_people(ctx, *[p.created_by for p in parts]))   # подпись «кто добавил» у каждой позиции
    open_issue = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status == "new"))
    can_pick = (ctx.can("parts.pick") and wo.status in ("picking", "waiting") and not wo.awaiting_prepay
                and (ctx.user.id == wo.partsman_id or ctx.can("orders.view_all")))
    # Деньги и работы
    from ..models import Payment, WoWorkSession
    from ..models.money import PAY_METHODS, PAY_PURPOSES
    from ..services import money as money_svc
    payments = db.scalars(select(Payment).where(Payment.work_order_id == wo.id).order_by(Payment.created_at)).all()
    people.update(_people(ctx, *[p.created_by for p in payments]))
    pauses = {s.wo_work_id: s for s in db.scalars(select(WoWorkSession).where(
        WoWorkSession.work_order_id == wo.id, WoWorkSession.kind == "pause", WoWorkSession.ended_at.is_(None)))}
    # Запчасти (11.11.2026): варианты на выбор клиенту — группами; менеджер вносит запчасти сам
    from ..models.orders import PART_OFF
    from ..services import brands as brands_svc
    from .stock import _can_choose, _can_offer
    groups: dict[str, list] = {}
    for p in parts:
        if p.option_group:
            groups.setdefault(p.option_group, []).append(p)
    plain = [p for p in parts if not p.option_group]
    options_open = sum(1 for g in groups.values() if any(p.status == "option" for p in g)
                       and not any(p.status not in ("option", "declined") for p in g))
    can_offer = _can_offer(ctx, wo)
    counted = [p for p in parts if p.status not in PART_OFF]
    free = stock_svc.catalog_free(db, ctx.branch.id, [p.catalog_id for p in parts if p.catalog_id]) if groups else {}
    n_done = sum(1 for w in works if w.status == "done")
    mc, pm = people.get(wo.mechanic_id), people.get(wo.partsman_id)
    flow = svc.flow(wo, mc.short_name if mc else "", pm.short_name if pm else "", len(findings), len(works), n_done,
                    options_open)
    is_manager = ctx.can("orders.approve")
    # Кто ведёт и кто что делал (11.11.2026)
    from ..services import order_feed
    feed = order_feed.build(db, wo)
    mg = people.get(wo.manager_id)
    managers = svc.staff_by_role(db, ctx.branch.id, "manager") if can_assign else []
    if can_assign and mg and mg not in managers:
        managers = [mg, *managers]
    return ctx.render(
        "orders/view.html", title=wo.number, wo=wo, v=db.get(Vehicle, wo.vehicle_id), c=db.get(Client, wo.client_id),
        lift=db.get(Lift, wo.lift_id) if wo.lift_id else None, photos=[p for p in photos if not p.finding_id], works=works, history=history, findings=findings,
        finding_photos={f.id: [p for p in photos if p.finding_id == f.id] for f in findings}, URGENCY=URGENCY,
        can_diag=ctx.can("orders.diagnose") and is_mech and (wo.status in ("diag", "approve", "work")
                                                              or (wo.status == "created" and is_manager)),
        can_finish_diag=ctx.can("orders.diagnose") and is_mech and wo.status == "diag",
        can_approve=ctx.can("orders.approve") and wo.status == "approve",
        can_route=is_manager and wo.status == "created" and wo.scenario == "B",
        parts=parts, plain_parts=plain, groups=groups, free=free, options_open=options_open,
        can_offer=can_offer, can_choose=_can_choose(ctx, wo), flow=flow,
        feed=feed, last_change=feed[0] if feed else None, managers=managers,
        team=[("ведёт", mg), ("запчастист", pm), ("слесарь", mc)],
        can_urgent=(wo.status in WO_ACTIVE and (ctx.user.id == wo.mechanic_id or is_manager or ctx.user.role.code == "admin")),
        BRANDS=brands_svc.names(db, ctx.branch.id) if (can_pick or can_offer) else [],
        diag_mechanics=svc.staff_by_role(db, ctx.branch.id, "mechanic") if is_manager and wo.status == "created" else [],
        diag_lifts=svc.lifts_with_load(db, ctx.branch.id) if is_manager and wo.status == "created" else [],
        cats=cats, can_pick=can_pick, PART_STATUS=stock_svc.PART_STATUS_LABEL,
        show_cost=ctx.can("prices.view_purchase"), show_price=ctx.can("prices.view_sale") or ctx.can("prices.view_purchase"),
        default_markup=st_.get(db, ctx.branch.id, "pricing")["default_markup"], open_issue=open_issue,
        can_request_issue=(ctx.can("stock.request_issue") and not open_issue and wo.status in ("instock", "work")
                           and any(p.status == "reserved" for p in parts)
                           and (ctx.can("orders.view_all") or ctx.user.id == wo.mechanic_id)),
        parts_total=sum((p.price * p.qty for p in counted), 0), n_counted=len(counted),
        t=money_svc.totals(db, wo), payments=payments, PAY_METHODS=PAY_METHODS, PAY_PURPOSES=PAY_PURPOSES,
        can_money=ctx.can("money.accept") and wo.status not in ("closed", "cancel"),
        can_edit_works=ctx.can("orders.approve") and wo.status in ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready"),
        can_work=(wo.status == "work" and (ctx.user.id == wo.mechanic_id or ctx.can("orders.view_all"))
                  and ctx.can("orders.view_own", "orders.view_all")),
        is_admin=ctx.user.role.code == "admin", pauses=pauses,
        all_mechanics=svc.staff_by_role(db, ctx.branch.id, "mechanic"),
        hour_rate=st_.get(db, ctx.branch.id, "pricing")["hour_rate"],
        people=people, STATUSES=WO_STATUSES, INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
        fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate, can_assign=can_assign,
        can_add_photo=(ctx.can("orders.create") or ctx.can("orders.diagnose")) and wo.status in WO_ACTIVE,
        can_cancel=ctx.can("orders.cancel") and wo.status in WO_ACTIVE,
        partsmen=svc.staff_by_role(db, ctx.branch.id, "partsman") if can_assign else [],
        mechanics=svc.staff_by_role(db, ctx.branch.id, "mechanic") if can_assign else [],
        lifts=svc.lifts_with_load(db, ctx.branch.id) if can_assign else [],
        **_order_tools(ctx, wo, works),
    )


def _order_tools(ctx: Ctx, wo, works) -> dict:
    """Общий инструмент под этот наряд: что выдано и (кладовщику) — что можно выдать."""
    from ..models.tools import TOOL_CATEGORIES
    from ..services import tools as tsvc
    if not ctx.can("tools.view", "tools.manage", "tools.view_own", "orders.view_all"):
        return {"wo_tools": [], "can_issue_tool": False}
    can_issue = ctx.can("tools.manage") and wo.status in WO_ACTIVE
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))
                           .order_by(User.full_name)).all() if can_issue else []
    holders = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return {"wo_tools": tsvc.for_order(ctx.db, wo.id), "can_issue_tool": can_issue,
            "free_tools": tsvc.available_shared(ctx.db, ctx.branch.id) if can_issue else [],
            "tool_staff": staff, "tool_holders": holders, "TOOL_CATEGORIES": TOOL_CATEGORIES,
            "works_by_id": {w.id: w for w in works}}


@router.post("/orders/{order_id}/tools")
def order_tool_issue(order_id: str, ctx: Ctx = Depends(get_ctx), tool_id: str = Form(...), user_id: str = Form(...),
                     work_id: str = Form("")):
    """Кладовщик выдаёт общий инструмент под этот наряд (и работу) — временно, до возврата."""
    ctx.need("tools.manage")
    wo = _order(ctx, order_id)
    from ..models import Tool
    from ..services import tools as tsvc
    t = ctx.db.get(Tool, _uuid(tool_id))
    u = ctx.db.get(User, _uuid(user_id))
    w = ctx.db.get(WoWork, _uuid(work_id)) if work_id else None
    if (not t or t.branch_id != ctx.branch.id or t.holder_id or t.is_personal or not u or u.branch_id != ctx.branch.id
            or not u.active or (w and w.work_order_id != wo.id)):
        flash(ctx.request, "Не получилось выдать: проверьте инструмент и сотрудника", "error")
    else:
        tsvc.move(ctx.db, t, u, f"Под наряд {wo.number}" + (f": {w.name}" if w else ""), wo=wo, work=w)
        ctx.db.commit()
        flash(ctx.request, f"{t.name} выдан: {u.full_name}")
    return RedirectResponse(f"/orders/{wo.id}", status_code=303)


@router.post("/orders/{order_id}/assign")
def order_assign(order_id: str, ctx: Ctx = Depends(get_ctx), partsman_id: str = Form(""),
                 mechanic_id: str = Form(""), lift_id: str = Form(""), manager_id: str = Form("")):
    ctx.need("orders.assign")
    wo = _order(ctx, order_id)
    bid = ctx.branch.id

    def pick(v, role):
        u = ctx.db.get(User, uuid.UUID(v)) if v else None
        return u.id if u and u.branch_id == bid and u.role.code == role else None

    wo.partsman_id, wo.mechanic_id = pick(partsman_id, "partsman"), pick(mechanic_id, "mechanic")
    # Передать наряд другому менеджеру (11.11.2026) — новому приходит сообщение
    new_mg = ctx.db.get(User, _uuid(manager_id)) if manager_id else None
    if new_mg and new_mg.branch_id == bid and new_mg.active and new_mg.id != wo.manager_id:
        wo.manager_id = new_mg.id
        from ..services import notify as nsvc
        nsvc.send(ctx.db, [new_mg.id], "handover", "Вам передан наряд",
                  f"{nsvc.car(ctx.db, wo)} · наряд {wo.number} · передал {ctx.user.short_name}", f"/orders/{wo.id}", wo,
                  skip=ctx.user.id)
    lift = ctx.db.get(Lift, uuid.UUID(lift_id)) if lift_id else None
    wo.lift_id = lift.id if lift and lift.branch_id == bid else None
    for w in ctx.db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id.is_(None))):
        w.mechanic_id = wo.mechanic_id
    if wo.status == "created":   # назначили недостающее — наряд едет дальше сам
        svc.route_after_intake(ctx.db, wo)
    ctx.db.commit()
    flash(ctx.request, "Назначения сохранены")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/photos")
async def order_add_photos(order_id: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.create", "orders.diagnose")
    wo = _order(ctx, order_id)
    form = await request.form(max_files=2 * svc.MAX_PHOTOS + 2)
    photos = [p for p in form.getlist("photos") if hasattr(p, "read")]
    thumbs = [p for p in form.getlist("thumbs") if hasattr(p, "read")]
    have = ctx.db.scalar(select(func.count()).select_from(WoPhoto).where(WoPhoto.work_order_id == wo.id))
    room = max(0, 60 - have)  # общий потолок на наряд (приёмка + диагностика + работа)
    stage = "diag" if wo.status == "diag" else ("work" if wo.status == "work" else "intake")
    saved = 0
    for i, p in enumerate(photos[:room]):
        thumb = await thumbs[i].read() if i < len(thumbs) else None
        if svc.save_photo(ctx.db, wo, await p.read(), thumb, stage):
            saved += 1
    ctx.db.commit()
    return {"ok": saved > 0, "saved": saved}


@router.post("/orders/{order_id}/cancel")
def order_cancel(order_id: str, ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    ctx.need("orders.cancel")
    wo = _order(ctx, order_id)
    if not reason.strip():
        flash(ctx.request, "Укажите причину отмены", "error")
    elif wo.status in WO_ACTIVE:
        wo.cancel_reason = reason.strip()[:500]
        svc.set_status(ctx.db, wo, "cancel", wo.cancel_reason)
        ctx.db.commit()
        flash(ctx.request, "Наряд отменён")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.get("/photos/{photo_id}")
def photo_file(photo_id: str, ctx: Ctx = Depends(get_ctx), t: int = 0):
    """Фото отдаются только тем, кто видит наряд."""
    p = ctx.db.get(WoPhoto, _uuid(photo_id))
    wo = ctx.db.get(WorkOrder, p.work_order_id) if p else None
    if not wo or not svc.can_view(ctx, wo):
        raise Forbidden()
    rel = p.thumb if (t and p.thumb) else p.file
    path = (svc.PHOTO_DIR / rel).resolve()
    if svc.PHOTO_DIR.resolve() not in path.parents or not path.exists():
        raise Forbidden()
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


# ---------- Клиенты ----------
@router.get("/clients")
def clients_list(ctx: Ctx = Depends(get_ctx), q: str = "", tab: str = "all", sort: str = "last", make: str = ""):
    """Клиенты: вкладки (все, постоянные, должники, давно не были, новые), сортировка, фильтр по марке, поиск."""
    ctx.need("clients.view")
    from ..services import clients as csvc
    tab = tab if tab in csvc.TABS else "all"
    sort = sort if sort in csvc.SORTS else "last"
    allrows = csvc.rows(ctx.db, ctx.branch.id)
    ids = csvc.search_ids(ctx.db, ctx.branch.id, q)
    base = csvc.apply(allrows, "all", sort, make, ids)
    items = csvc.apply(allrows, tab, sort, make, ids)
    from urllib.parse import urlencode

    def link(**over):
        d = {"q": q, "tab": tab, "sort": sort, "make": make, **over}
        return "/clients?" + urlencode({k: v for k, v in d.items() if v and not (k == "tab" and v == "all")
                                        and not (k == "sort" and v == "last")})
    return ctx.render("clients/list.html", title="Клиенты", items=items, q=q, tab=tab, sort=sort, make=make,
                      tabs=csvc.TABS, sorts=csvc.SORTS, counts=csvc.counts(base), makes=csvc.makes(allrows), link=link,
                      show_money=ctx.can("money.view"), fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate,
                      REGULAR_VISITS=csvc.REGULAR_VISITS, LOST_DAYS=csvc.LOST_DAYS)


@router.post("/clients/{client_id}/regular")
def client_regular(client_id: str, ctx: Ctx = Depends(get_ctx)):
    """Отметить / снять «Постоянный клиент» вручную."""
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    c.is_regular = not bool(c.is_regular)
    ctx.db.commit()
    flash(ctx.request, "Отмечен как постоянный" if c.is_regular else "Отметка «постоянный» снята")
    return RedirectResponse(f"/clients/{c.id}", status_code=303)


@router.get("/clients/{client_id}")
def client_view(client_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    vehicles = ctx.db.scalars(select(Vehicle).where(Vehicle.client_id == c.id).order_by(Vehicle.created_at)).all()
    orders = ctx.db.scalars(svc.visible_orders_query(ctx).where(WorkOrder.client_id == c.id)
                            .order_by(WorkOrder.created_at.desc())).all()
    events = ctx.db.scalars(select(ClientEvent).where(ClientEvent.client_id == c.id)
                            .order_by(ClientEvent.created_at.desc()).limit(300)).all()
    recs = [e for e in events if e.kind == "recommendation" and not e.resolved]
    numbers = {o.id: o.number for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.client_id == c.id))}
    from ..services import clients as csvc
    return ctx.render("clients/view.html", title=c.full_name, c=c, vehicles=vehicles, cards=_cards(ctx, orders),
                      st=csvc.one(ctx.db, c), REGULAR_VISITS=csvc.REGULAR_VISITS, show_money=ctx.can("money.view"),
                      STATUSES=WO_STATUSES, fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate, events=events,
                      recs=recs, people=_people(ctx, *[e.created_by for e in events]), URGENCY=URGENCY,
                      vmap={v.id: v for v in vehicles}, numbers=numbers,
                      lead=ctx.db.get(User, orders[0].manager_id) if orders and orders[0].manager_id else None)
