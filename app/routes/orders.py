"""Наряды: список, приёмка (новый наряд), карточка, фото, клиенты."""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select

from ..models import Client, ClientEvent, Lift, User, Vehicle, WoFinding, WoPhoto, WorkOrder, WoStatusHistory, WoWork
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, URGENCY, WO_ACTIVE, WO_STATUSES
from ..services import orders as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from ..permissions import MASTERS, is_master

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
# Этапы для плиток списка нарядов: ключ → (заголовок, цвет, статусы)
ORDER_STAGES = {
    "active": ("На СТО", "blue", WO_ACTIVE),
    "intake": ("Приём и диагностика", "purple", ("created", "diag")),
    "approve": ("Согласование", "yellow", ("approve",)),
    "parts": ("Запчасти", "orange", ("picking", "waiting", "instock")),
    "work": ("В работе", "teal", ("work",)),
    "ready": ("Готовы к выдаче", "green", ("ready", "paid")),
    "closed": ("Закрыты", "gray", ("closed",)),
}


@router.get("/orders")
def orders_list(ctx: Ctx = Depends(get_ctx), tab: str = "active", q: str = "", status: str = "",
                p: str = "", d: str = "", f: str = "", t: str = "", found: str = ""):
    ctx.need("orders.view_all", "orders.view_own")
    # 12.11.2026: менеджер видит свои наряды (и 3 дня после выдачи). Клиент вернулся (гарантия) — ищет по ТОЧНЫМ данным
    # (полный госномер / телефон / VIN / номер наряда): найденный клиент и все его наряды открыты ему 1 час, поиск — в журнал.
    if q.strip() and not found and svc.manager_limited(ctx):
        from ..services import decisions as dsvc
        hits = dsvc.exact_clients(ctx.db, ctx.branch.id, q)
        dsvc.grant_by_search(ctx.db, ctx, hits, q)
        ctx.db.commit()
        if hits:
            from urllib.parse import urlencode
            return RedirectResponse("/orders?" + urlencode({"tab": "all", "q": q.strip(), "found": "1", "p": "all"}), status_code=303)
    from ..services import periods
    from ..services.attendance import day_bounds
    per = periods.parse(p, d, f, t, default="all", allowed=("day", "week", "month", "quarter", "year", "custom", "all"))
    query = svc.visible_orders_query(ctx)
    if per.kind != "all":   # принятые за период
        from datetime import timedelta
        query = query.where(WorkOrder.created_at >= day_bounds(per.d1)[0],
                            WorkOrder.created_at < day_bounds(per.d2)[0] + timedelta(days=1))
    # Плитки этапов как на «Пульте» (11.11.2026): сколько нарядов на каждом этапе (с учётом периода, без поиска)
    by_status = {}
    for st_, in ctx.db.execute(query.with_only_columns(WorkOrder.status)):
        by_status[st_] = by_status.get(st_, 0) + 1
    stage_counts = {k: sum(by_status.get(s, 0) for s in v[2]) for k, v in ORDER_STAGES.items()}
    stage_counts["all"] = sum(by_status.values())
    # 12.11.2026: «Мне передали» — наряды, которые передали мне и я ещё не нажал «Принял»
    handed_cond = (WorkOrder.manager_id == ctx.user.id, WorkOrder.handover_pending.is_(True), WorkOrder.status.in_(WO_ACTIVE))
    show_handed = ctx.can("orders.approve")
    stage_counts["handed"] = ctx.db.scalar(select(func.count()).select_from(WorkOrder).where(*handed_cond)) or 0 if show_handed else 0
    # 12.11.2026: «Пришли запчасти» — мои наряды, где запчасти пришли по закупке и ещё не выданы (зелёная цифра в меню)
    from ..services.notify import arrived_waiting
    arrived = arrived_waiting(ctx.db, ctx.branch.id, ctx.user.id)
    stage_counts["arrived"] = len(arrived)
    if tab == "handed" and show_handed:
        query = query.where(*handed_cond)
    elif tab == "arrived":
        query = query.where(WorkOrder.id.in_(list(arrived) or [uuid.uuid4()]))
    elif tab in ORDER_STAGES:
        query = query.where(WorkOrder.status.in_(ORDER_STAGES[tab][2]))
    elif status in WO_STATUSES:
        query = query.where(WorkOrder.status == status)
    elif tab == "active" and not q.strip():   # с поиском — и закрытые тоже
        query = query.where(WorkOrder.status.in_(WO_ACTIVE))
    q = q.strip()
    if q:
        plate, digits = svc.norm_plate(q), "".join(ch for ch in q if ch.isdigit())
        conds = [WorkOrder.number.contains(q), Vehicle.plate.contains(plate), Client.full_name.ilike(f"%{q}%")]
        if len(q) >= 4:
            conds.append(Vehicle.vin.contains(q.upper().replace(" ", "")))
        if len(digits) >= 3:
            conds.append(Client.phone.contains(digits))
        query = (query.join(Vehicle, Vehicle.id == WorkOrder.vehicle_id).join(Client, Client.id == WorkOrder.client_id)
                 .where(or_(*conds)))
    orders = ctx.db.scalars(query.order_by(WorkOrder.created_at.desc()).limit(200)).all()
    lifts = []
    if ctx.can("lifts.take") and is_master(ctx.user):
        lifts = ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id, Lift.active.is_(True))
                               .order_by(Lift.number)).all()
    # 12.11.2026: менеджер видит свои наряды; ко всем — по запросу на срок
    acc = None
    if ctx.user.role.code == "manager" and ctx.can("orders.view_all"):
        from ..models import OrderAccess
        from ..services import decisions as dsvc
        pend = ctx.db.scalar(select(OrderAccess.id).where(OrderAccess.user_id == ctx.user.id, OrderAccess.status == "pending"))
        acc = {"until": svc.access_until(ctx.db, ctx.user.id), "pending": pend,
               "self_ok": bool(pend) and dsvc.can_self_decide(ctx.db, ctx.branch.id)}
    return ctx.render("orders/list.html", title="Наряды", cards=_cards(ctx, orders), tab=tab, q=q, acc=acc,
                      STATUSES=WO_STATUSES, fmt_plate=svc.fmt_plate, status=status, my_lifts=lifts,
                      lift_people=_people(ctx, *[l.mechanic_id for l in lifts]), per=per,
                      STAGES=ORDER_STAGES, stage_counts=stage_counts, show_handed=show_handed,
                      bar=periods.Bar(per, "/orders", {"tab": tab, "q": q, "status": status}))


@router.post("/lifts/take")
def lift_take(ctx: Ctx = Depends(get_ctx), lift_id: str = Form("")):
    """Слесарь сам закрепляется за подъёмником (или открепляется — пустое значение).
    Занятый другим слесарем пост забрать нельзя — это решает админ в настройках."""
    ctx.need("lifts.take")
    if not is_master(ctx.user):
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
                      mechanics=svc.staff_by_role(ctx.db, bid, "masters"),
                      lifts=svc.lifts_with_load(ctx.db, bid), makes=svc.known_makes(ctx.db, bid),
                      INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS, MAX_PHOTOS=svc.MAX_PHOTOS,
                      fmt_plate=svc.fmt_plate, prefill=prefill, intake_token=uuid.uuid4().hex)


@router.post("/orders/new")
async def order_create(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Форма приёмки отправляется из JS (FormData): поля + сжатые на телефоне фото и миниатюры."""
    ctx.need("orders.create")
    form = await request.form(max_files=2 * svc.MAX_PHOTOS + 2)
    f = svc.IntakeForm(**{k: str(form.get(k, "")) for k in svc.IntakeForm.__dataclass_fields__ if k != "intake_items"},
                       intake_items=[str(x) for x in form.getlist("intake_items")])
    photos = [p for p in form.getlist("photos") if hasattr(p, "read")][:svc.MAX_PHOTOS]
    thumbs = [p for p in form.getlist("thumbs") if hasattr(p, "read")]
    errors = f.errors(len(photos)) or f.problems(ctx.db, ctx.branch.id)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    # Та же форма пришла второй раз (двойное нажатие, медленный Wi-Fi) — отдаём уже созданный наряд
    token = str(form.get("intake_token", "")).strip()[:40] or None
    if token:
        same = ctx.db.scalar(select(WorkOrder).where(WorkOrder.intake_token == token))
        if same:
            return {"ok": True, "url": f"/orders/{same.id}"}

    wo = svc.create_order(ctx.db, ctx, f)
    wo.intake_token = token
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
    from sqlalchemy.exc import IntegrityError
    try:
        ctx.db.commit()
    except IntegrityError:   # два одновременных запроса с одной формы — второй упёрся в метку
        ctx.db.rollback()
        same = ctx.db.scalar(select(WorkOrder).where(WorkOrder.intake_token == token)) if token else None
        if same:
            return {"ok": True, "url": f"/orders/{same.id}"}
        raise
    q = getattr(wo, "queued_for", None)
    flash(request, f"Наряд {wo.number} создан" + (f" — подъёмник {q} занят, машина в очереди" if q else ""))
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
    # открытый запрос на выдачу: ждёт кладовщика или «Готово к выдаче» (12.11.2026) — сначала готовый
    open_issue = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status.in_(("new", "ready")))
                           .order_by(IssueRequest.status.desc()))
    can_pick = (ctx.can("parts.pick") and wo.status in ("picking", "waiting") and not wo.awaiting_prepay
                and (ctx.user.id == wo.partsman_id or ctx.can("orders.view_all")))
    # Деньги и работы
    from ..models import Payment, WoWorkSession
    from ..models.money import PAY_IN, PAY_METHODS, PAY_PURPOSES
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
        team=[("ведёт", mg), ("запчастист", pm), (MASTERS.get(mc.role.code, "мастер").lower() if mc else "мастер", mc)],
        can_urgent=(wo.status in WO_ACTIVE and (ctx.user.id == wo.mechanic_id or is_manager or ctx.user.role.code == "admin")),
        BRANDS=brands_svc.names(db, ctx.branch.id) if (can_pick or can_offer) else [],
        diag_mechanics=svc.staff_by_role(db, ctx.branch.id, "masters") if is_manager and wo.status == "created" else [],
        diag_lifts=svc.lifts_with_load(db, ctx.branch.id) if is_manager and wo.status == "created" else [],
        cats=cats, can_pick=can_pick, PART_STATUS=stock_svc.PART_STATUS_LABEL,
        show_cost=ctx.can("prices.view_purchase"), show_price=ctx.can("prices.view_sale") or ctx.can("prices.view_purchase"),
        default_markup=st_.get(db, ctx.branch.id, "pricing")["default_markup"], open_issue=open_issue,
        can_request_issue=(ctx.can("stock.request_issue") and not open_issue and wo.status in ("instock", "work")
                           and any(p.status == "reserved" for p in parts)
                           and (ctx.can("orders.view_all") or ctx.user.id == wo.mechanic_id)),
        parts_total=sum((p.price * p.qty for p in counted), 0), n_counted=len(counted),
        t=money_svc.totals(db, wo), payments=payments, PAY_METHODS=PAY_METHODS, PAY_PURPOSES=PAY_PURPOSES, PAY_IN=PAY_IN,
        pay_token=uuid.uuid4().hex,
        refund_max=0 if svc.history_only(ctx, wo) else money_svc.refund_limit(db, wo),
        history_only=svc.history_only(ctx, wo),
        left_parts=money_svc.not_issued_parts(db, wo) if wo.status in ("ready", "paid") else [],
        reopen=_reopen_state(ctx, wo),
        order_msgs=_order_msgs(ctx, wo),
        draft_parts=stock_svc.draft_part_ids(db, [p.id for p in parts if p.status == "ordered"]),
        handed=({"from": db.get(User, wo.handover_from) if wo.handover_from else None, "at": wo.handover_at}
                if wo.handover_pending and wo.manager_id == ctx.user.id and wo.status in WO_ACTIVE else None),
        can_money=ctx.can("money.accept") and (wo.status not in ("closed", "cancel")
                                               or (wo.status == "closed" and money_svc.totals(db, wo).due > 0)),
        can_edit_works=ctx.can("orders.approve") and wo.status in ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready"),
        can_work=(wo.status == "work" and (ctx.user.id == wo.mechanic_id or ctx.can("orders.view_all"))
                  and ctx.can("orders.view_own", "orders.view_all")),
        # 12.11.2026: второй мастер делает свою работу, не дожидаясь запчастей ведущего (мойка не ждёт колодки)
        own_work_ok={w.id for w in works if w.mechanic_id == ctx.user.id and svc.can_start_work(db, wo, w)},
        is_admin=ctx.user.role.code == "admin", pauses=pauses,
        all_mechanics=svc.staff_by_role(db, ctx.branch.id, "masters"),
        hour_rate=st_.get(db, ctx.branch.id, "pricing")["hour_rate"],
        people=people, STATUSES=WO_STATUSES, INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
        fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate, can_assign=can_assign,
        can_add_photo=(ctx.can("orders.create") or ctx.can("orders.diagnose")) and wo.status in WO_ACTIVE,
        can_cancel=ctx.can("orders.cancel") and wo.status in WO_ACTIVE and wo.status != "paid",
        partsmen=svc.staff_by_role(db, ctx.branch.id, "partsman") if can_assign else [],
        mechanics=svc.staff_by_role(db, ctx.branch.id, "masters") if can_assign else [],
        lifts=svc.lifts_with_load(db, ctx.branch.id) if can_assign else [],
        **_order_tools(ctx, wo, works),
    )


def _order_msgs(ctx: Ctx, wo) -> dict:
    """Сообщения по этой машине (11.11.2026) — переписка команды и запросы запчастей слесарем."""
    from ..models import PartCatalog
    from ..services import messages as msg
    rows = msg.for_order(ctx.db, wo)
    t = msg.team(ctx.db, wo)
    return {"rows": rows, "team": t, "people": _people(ctx, *[m.created_by for m in rows], *[m.decided_by for m in rows]),
            "cats": {c.id: c for c in ctx.db.scalars(select(PartCatalog).where(PartCatalog.id.in_([m.catalog_id for m in rows if m.catalog_id])))},
            "can_write": wo.status in msg.OPEN_STATUSES,
            "can_ask": ctx.can("parts.ask") and wo.status in msg.ASK_STATUSES
                       and wo.id in {o.id for o in msg.my_orders(ctx)}}


def _order_tools(ctx: Ctx, wo, works) -> dict:
    """Общий инструмент под этот наряд: что выдано и (кладовщику) — что можно выдать."""
    from ..models import Tool
    from ..models.tools import TOOL_CATEGORIES
    from ..services import tools as tsvc
    if not ctx.can("tools.view", "tools.manage", "tools.view_own", "orders.view_all"):
        return {"wo_tools": [], "can_issue_tool": False, "can_request_tool": False, "tool_pending": []}
    can_issue = ctx.can("tools.manage") and wo.status in WO_ACTIVE
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))
                           .order_by(User.full_name)).all() if can_issue else []
    holders = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    # Слесарь сам просит инструмент и сдаёт его (11.11.2026) — у кладовщика всплывает окошко
    from ..models import ToolRequest
    # 12.11.2026: попросить может и второй мастер этой машины (своя работа в наряде), не только ведущий
    can_req = wo.status in WO_ACTIVE and tsvc.can_request_for_order(ctx.db, ctx.user, wo)
    pending = ctx.db.scalars(select(ToolRequest).where(ToolRequest.work_order_id == wo.id, ToolRequest.status == "new")
                             .order_by(ToolRequest.created_at)).all()
    asked = {r.tool_id for r in ctx.db.scalars(select(ToolRequest).where(ToolRequest.branch_id == ctx.branch.id,
                                                                         ToolRequest.status == "new"))}
    free = tsvc.available_shared(ctx.db, ctx.branch.id) if (can_issue or can_req) else []
    return {"wo_tools": tsvc.for_order(ctx.db, wo.id), "can_issue_tool": can_issue, "can_request_tool": can_req,
            "tool_pending": pending, "tool_pending_ids": {r.tool_id: r for r in pending},
            "tool_req_free": [t for t in free if t.id not in asked],
            "tool_names": {t.id: t for t in ctx.db.scalars(select(Tool).where(Tool.id.in_([r.tool_id for r in pending])))} if pending else {},
            "free_tools": free if can_issue else [],
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
        uid = svc._uuid(v)   # мусор в форме — просто «не выбрано», а не ошибка сервера
        u = ctx.db.get(User, uid) if uid else None
        ok = u and u.branch_id == bid and (u.role.code in MASTERS if role == "masters" else u.role.code == role)
        return u.id if ok else None

    if wo.status not in WO_ACTIVE:
        raise Forbidden()
    old_mech = wo.mechanic_id
    lift_uuid = svc._uuid(lift_id)
    lift = ctx.db.get(Lift, lift_uuid) if lift_uuid else None
    busy = svc.lift_busy(ctx.db, lift, wo) if lift and lift.branch_id == bid else None
    if busy:
        flash(ctx.request, f"На подъёмнике {lift.number} уже стоит машина (наряд {busy.number}) — сначала освободите его", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    wo.partsman_id, wo.mechanic_id = pick(partsman_id, "partsman"), pick(mechanic_id, "masters")
    # Передать наряд другому менеджеру (11.11.2026) — новому приходит сообщение. Передаёт только менеджер (и админ).
    new_uuid = svc._uuid(manager_id)
    new_mg = ctx.db.get(User, new_uuid) if new_uuid else None
    if new_mg and new_mg.id != wo.manager_id and ctx.user.role.code not in ("manager", "admin"):
        flash(ctx.request, "Передать наряд другому менеджеру может только менеджер", "error")
        new_mg = None
    if new_mg and new_mg.branch_id == bid and new_mg.active and new_mg.id != wo.manager_id:
        from ..db import now as _now
        wo.handover_from, wo.handover_at, wo.handover_pending = wo.manager_id, _now(), True
        wo.manager_id = new_mg.id
        from ..services import notify as nsvc
        nsvc.send(ctx.db, [new_mg.id], "handover", "Вам передан наряд",
                  f"{nsvc.car(ctx.db, wo)} · наряд {wo.number} · передал {ctx.user.short_name}", f"/orders/{wo.id}", wo,
                  skip=ctx.user.id)
    wo.lift_id = lift.id if lift and lift.branch_id == bid else None
    # Несделанные работы прежнего слесаря переходят новому — зарплата тому, кто делает (отчёт тестировщика 11.11.2026)
    for w in ctx.db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.status != "done")):
        if w.mechanic_id is None or (old_mech and w.mechanic_id == old_mech and wo.mechanic_id != old_mech):
            w.mechanic_id = wo.mechanic_id
    if wo.status == "created":   # назначили недостающее — наряд едет дальше сам
        svc.route_after_intake(ctx.db, wo)
    ctx.db.commit()
    if not svc.can_view(ctx, wo):   # передал наряд другому менеджеру — у себя он больше не виден (12.11.2026)
        flash(ctx.request, f"Наряд {wo.number} передан: {new_mg.full_name if new_mg else ''}")
        return RedirectResponse("/orders", status_code=303)
    flash(ctx.request, "Назначения сохранены")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/handover-ack")
def order_handover_ack(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """Менеджер принял переданный ему наряд (12.11.2026): плитка «Мне передали» и цифра в меню гаснут."""
    from ..db import now
    from ..models import Notification
    wo = _order(ctx, order_id)
    if wo.manager_id != ctx.user.id:
        raise Forbidden()
    wo.handover_pending = False
    for n in ctx.db.scalars(select(Notification).where(Notification.user_id == ctx.user.id, Notification.work_order_id == wo.id,
                                                       Notification.kind == "handover", Notification.read_at.is_(None))):
        n.read_at = now()
    svc.event(ctx.db, wo, "status", f"Наряд принял {ctx.user.full_name}")
    ctx.db.commit()
    flash(ctx.request, f"Наряд {wo.number} принят — теперь он ваш")
    return RedirectResponse(f"/orders/{wo.id}", status_code=303)


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
def order_cancel(order_id: str, ctx: Ctx = Depends(get_ctx), reason: str = Form(""), keep_money: str = Form("")):
    """Отмена наряда (11.11.2026 — всё возвращается): резервы снимаются, выданное — на склад, заказанное придёт
    на свободный склад. Оплаченный наряд не отменяется. Деньги — сначала возврат, или явно «оставляем СТО»."""
    ctx.need("orders.cancel")
    wo = _order(ctx, order_id)
    from ..services import money as msvc
    from ..services import stock as ssvc
    paid = msvc.totals(ctx.db, wo).paid
    if not reason.strip():
        flash(ctx.request, "Укажите причину отмены", "error")
    elif wo.status in ("paid",):
        flash(ctx.request, "Оплаченный наряд не отменяется: выдайте машину или сначала оформите возврат денег", "error")
    elif wo.status not in WO_ACTIVE:
        flash(ctx.request, "Этот наряд уже закрыт или отменён", "error")
    elif paid > 0 and not keep_money:
        flash(ctx.request, f"Клиент заплатил {paid} ₴: сначала оформите возврат (блок «Деньги») "
                           f"или отметьте «деньги остаются СТО»", "error")
    else:
        n = ssvc.release_order(ctx.db, wo)
        from ..services import payroll as psvc
        psvc.cancel_for_order(ctx.db, wo)
        wo.cancel_reason = reason.strip()[:500]
        if paid > 0:
            wo.cancel_reason += f" · деньги {paid} ₴ остаются СТО"
        svc.set_status(ctx.db, wo, "cancel", wo.cancel_reason)
        ctx.db.commit()
        flash(ctx.request, "Наряд отменён" + (f" — запчасти вернулись на склад: {n}" if n else ""))
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# ======================= Открыть наряд заново — по запросу (11.11.2026) =======================
# Просит любой, кто видит наряд; решают админ / директор / зам (orders.grant_access) — как доступ ко всем нарядам.
def _reopen_state(ctx: Ctx, wo: WorkOrder) -> dict | None:
    if wo.status not in ("closed", "cancel"):
        return None
    from ..models import OrderReopen
    pending = ctx.db.scalar(select(OrderReopen).where(OrderReopen.work_order_id == wo.id, OrderReopen.status == "pending"))
    from ..services import decisions as dsvc
    # 12.11.2026: директора и зама нет в здании (по табелю) — «открыть заново» (не гарантию) может решить сам тот, кто просил
    self_ok = bool(pending and pending.kind != "warranty" and pending.user_id == ctx.user.id
                   and dsvc.reopen_self_ok(wo) and dsvc.can_self_decide(ctx.db, ctx.branch.id))
    return {"pending": pending, "can_decide": ctx.can("orders.grant_access"), "self_ok": self_ok,
            "warranty_ok": wo.status == "closed" and ctx.can("orders.approve", "orders.grant_access")}


@router.post("/orders/{order_id}/reopen-request")
def reopen_request(order_id: str, ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    from ..models import OrderReopen
    from ..services import notify as nsvc
    wo = _order(ctx, order_id)
    if wo.status not in ("closed", "cancel"):
        raise Forbidden()
    if not reason.strip():
        flash(ctx.request, "Напишите, зачем открыть наряд заново — это увидит админ или директор", "error")
        return _to(f"/orders/{wo.id}")
    if ctx.db.scalar(select(OrderReopen.id).where(OrderReopen.work_order_id == wo.id, OrderReopen.status == "pending")):
        flash(ctx.request, "Запрос уже отправлен — ждём ответа")
        return _to(f"/orders/{wo.id}")
    ctx.db.add(OrderReopen(work_order_id=wo.id, user_id=ctx.user.id, reason=reason.strip()[:300], from_status=wo.status))
    nsvc.send(ctx.db, [u.id for u in _deciders(ctx)], "reopen_req", f"{ctx.user.full_name} просит открыть наряд {wo.number}",
              reason.strip()[:300], "/access-requests#reopen", wo, skip=ctx.user.id)
    ctx.db.commit()
    flash(ctx.request, "Запрос отправлен админу и директору — ответ придёт сообщением")
    return _to(f"/orders/{wo.id}")


@router.post("/orders/{order_id}/warranty-request")
def warranty_request(order_id: str, ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    """«Ремонт по гарантии»: клиент вернулся — запрос директору и заму (решают только они, гарантию всегда разбирают)."""
    from ..models import OrderReopen
    from ..services import notify as nsvc
    ctx.need("orders.approve", "orders.grant_access")
    wo = _order(ctx, order_id)
    if wo.status != "closed":
        raise Forbidden()
    if not reason.strip():
        flash(ctx.request, "Опишите, с чем вернулся клиент — это увидят директор и зам", "error")
        return _to(f"/orders/{wo.id}")
    if ctx.db.scalar(select(OrderReopen.id).where(OrderReopen.work_order_id == wo.id, OrderReopen.status == "pending")):
        flash(ctx.request, "Запрос уже отправлен — ждём ответа")
        return _to(f"/orders/{wo.id}")
    ctx.db.add(OrderReopen(work_order_id=wo.id, user_id=ctx.user.id, reason=reason.strip()[:300], from_status=wo.status,
                           kind="warranty"))
    nsvc.send(ctx.db, [u.id for u in _deciders(ctx)], "reopen_req", f"Гарантия: {ctx.user.full_name} просит открыть наряд {wo.number}",
              reason.strip()[:300], "/access-requests#reopen", wo, skip=ctx.user.id)
    ctx.db.commit()
    flash(ctx.request, "Запрос «Ремонт по гарантии» отправлен директору и заму — ответ придёт сообщением")
    return _to(f"/orders/{wo.id}")


@router.post("/reopen-requests/{rid}/self")
def reopen_self(rid: str, ctx: Ctx = Depends(get_ctx)):
    """Директора и зама нет в здании — «открыть заново» (наряд закрыт не больше суток назад) решает сам тот, кто просил."""
    from ..db import now
    from ..models import OrderReopen
    from ..services import decisions as dsvc
    r = ctx.db.get(OrderReopen, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id or r.status != "pending" or r.user_id != ctx.user.id or r.kind == "warranty":
        raise Forbidden()
    wo = ctx.db.get(WorkOrder, r.work_order_id)
    if not dsvc.can_self_decide(ctx.db, ctx.branch.id):
        flash(ctx.request, "Директор или зам на месте (по табелю) — найдите их, решают они", "error")
        return _to(f"/orders/{wo.id}")
    if not dsvc.reopen_self_ok(wo):
        flash(ctx.request, "Сам можно открыть только наряд, закрытый не больше суток назад — ждите директора или зама", "error")
        return _to(f"/orders/{wo.id}")
    r.status, r.decided_by, r.decided_at, r.self_decided = "approved", ctx.user.id, now(), True
    to = svc.reopen(ctx.db, wo)
    note = dsvc.absent_note(ctx.db, ctx.branch.id)
    svc.event(ctx.db, wo, "status", f"Открыт заново: решил(а) сам(а) {ctx.user.full_name} — {note}. Причина: {r.reason}")
    dsvc.tell_deciders(ctx.db, ctx, f"открыт заново наряд {wo.number}", f"Причина: {r.reason}", f"/orders/{wo.id}", wo)
    ctx.db.commit()
    flash(ctx.request, f"Наряд {wo.number} открыт заново — {WO_STATUSES[to]}. Директору и заму отправлено сообщение")
    return _to(f"/orders/{wo.id}")


@router.post("/orders/{order_id}/warranty-done")
def warranty_done(order_id: str, ctx: Ctx = Depends(get_ctx), result: str = Form(""), action: str = Form("done")):
    """«Выполнены гарантийные работы» (закрыть второй раз) или «Не гарантийный случай»."""
    from ..services import warranty as wsvc
    ctx.need("orders.approve", "orders.grant_access")
    wo = _order(ctx, order_id)
    err = (wsvc.reject if action == "reject" else wsvc.done)(ctx.db, wo, ctx.user, result)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Гарантийный ремонт выполнен — наряд закрыт второй раз" if action != "reject"
              else "Не гарантийный случай — наряд закрыт как был. Ремонт оформите новым нарядом через приёмку")
    return _to(f"/orders/{order_id}")


@router.post("/reopen-requests/{rid}")
def reopen_decide(rid: str, ctx: Ctx = Depends(get_ctx), action: str = Form("")):
    from ..db import now
    from ..models import Notification, OrderReopen
    from ..services import notify as nsvc
    ctx.need("orders.grant_access")
    r = ctx.db.get(OrderReopen, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id:
        raise Forbidden()
    wo = ctx.db.get(WorkOrder, r.work_order_id)
    back = ctx.request.headers.get("referer") or "/access-requests"
    if r.status != "pending":
        flash(ctx.request, "По этому запросу уже решили", "error")
        return _to(back)
    r.decided_by, r.decided_at = ctx.user.id, now()
    if action == "approve" and r.kind == "warranty" and wo.status == "closed":
        r.status = "approved"
        from ..services import warranty as wsvc
        wsvc.open_(ctx.db, wo, r.reason)
        to = wo.status
        nsvc.send(ctx.db, [r.user_id, wo.manager_id], "reopen_ok", f"Наряд {wo.number} открыт по гарантии",
                  f"Разрешил: {ctx.user.full_name}. Добавьте гарантийные работы (0 ₴) и мастера", f"/orders/{wo.id}", wo)
        flash(ctx.request, f"Наряд {wo.number} открыт по гарантии")
    elif action == "approve" and wo.status in ("closed", "cancel"):
        r.status = "approved"
        to = svc.reopen(ctx.db, wo)
        nsvc.send(ctx.db, [r.user_id, wo.manager_id], "reopen_ok", f"Наряд {wo.number} открыт заново",
                  f"Разрешил: {ctx.user.full_name} · сейчас: {WO_STATUSES[to]}", f"/orders/{wo.id}", wo)
        flash(ctx.request, f"Наряд {wo.number} открыт заново — {WO_STATUSES[to]}")
    else:
        r.status = "declined"
        nsvc.send(ctx.db, [r.user_id], "reopen_no", f"Наряд {wo.number}: открыть заново — отказано",
                  f"Ответил: {ctx.user.full_name}", f"/orders/{wo.id}", wo)
        flash(ctx.request, "Отказано")
    for n in ctx.db.scalars(select(Notification).where(Notification.kind == "reopen_req", Notification.read_at.is_(None),
                                                       Notification.work_order_id == wo.id)):
        n.read_at = now()
    ctx.db.commit()
    return _to(back)


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
    # 12.11.2026: менеджер видит только своих клиентов (машина у нас или выдана не больше 3 дней назад) и тех,
    # к кому ему дали доступ. Найти любого клиента для новой приёмки можно в «Приёмке» — поиск там по всей базе.
    vis = svc.visible_client_ids(ctx)
    if vis is not None:
        allrows = [r for r in allrows if r.c.id in vis]
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
                      REGULAR_VISITS=csvc.REGULAR_VISITS, LOST_DAYS=csvc.LOST_DAYS, limited=vis is not None,
                      RECENT_DAYS=svc.RECENT_DAYS, my_access=_my_client_access(ctx) if vis is not None else None)


def _my_client_access(ctx: Ctx) -> dict:
    """Менеджеру: доступы к клиентам, что сейчас действуют, и есть ли запрос, ждущий решения."""
    from datetime import datetime, timezone
    from ..models import ClientAccess
    rows = ctx.db.scalars(select(ClientAccess).where(ClientAccess.user_id == ctx.user.id, ClientAccess.status == "approved",
                                                     ClientAccess.until > datetime.now(timezone.utc))).all()
    clients = {c.id: c for c in ctx.db.scalars(select(Client).where(Client.id.in_([r.client_id for r in rows])))}
    return {"active": [(r, clients.get(r.client_id)) for r in rows],
            "pending": ctx.db.scalar(select(ClientAccess.id).where(ClientAccess.user_id == ctx.user.id,
                                                                   ClientAccess.status == "pending")) is not None}


def _client_access_lists(ctx: Ctx) -> dict:
    from datetime import datetime, timezone
    from ..models import ClientAccess
    pend = ctx.db.scalars(select(ClientAccess).where(ClientAccess.branch_id == ctx.branch.id, ClientAccess.status == "pending")
                          .order_by(ClientAccess.created_at)).all()
    act = ctx.db.scalars(select(ClientAccess).where(ClientAccess.branch_id == ctx.branch.id, ClientAccess.status == "approved",
                                                    ClientAccess.until > datetime.now(timezone.utc))).all()
    cl = {c.id: c for c in ctx.db.scalars(select(Client).where(Client.id.in_([a.client_id for a in act])))}
    return {"c_pending": pend, "c_active": act, "c_clients": cl}


def _grant_data(ctx: Ctx, c) -> dict:
    """Для директора / зама / админа в карточке клиента: кому дать доступ, ждущие запросы, кому уже дан."""
    from datetime import datetime, timezone
    from ..models import ClientAccess
    from ..models.orders import CLIENT_ACCESS_HOURS
    managers = svc.staff_by_role(ctx.db, ctx.branch.id, "manager")
    pend = ctx.db.scalars(select(ClientAccess).where(ClientAccess.branch_id == ctx.branch.id, ClientAccess.status == "pending")
                          .order_by(ClientAccess.created_at)).all()
    active = ctx.db.scalars(select(ClientAccess).where(ClientAccess.client_id == c.id, ClientAccess.status == "approved",
                                                       ClientAccess.until > datetime.now(timezone.utc))).all()
    people = {u.id: u for u in managers}
    return {"managers": managers, "pending": pend, "active": active, "people": people, "HOURS": CLIENT_ACCESS_HOURS}


@router.post("/clients/access-request")
def client_access_request(ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    """Менеджеру срочно нужен «чужой» клиент: пишет, кого ищет и зачем → директору / заму / админу (12.11.2026)."""
    from ..models import ClientAccess
    from ..services import notify as nsvc
    ctx.need("clients.view")
    if not svc.manager_limited(ctx):
        return _to("/clients")
    if not reason.strip():
        flash(ctx.request, "Напишите, кого ищете (имя, телефон, госномер) и зачем", "error")
        return _to("/clients")
    if ctx.db.scalar(select(ClientAccess.id).where(ClientAccess.user_id == ctx.user.id, ClientAccess.status == "pending")):
        flash(ctx.request, "Запрос уже отправлен — ждём ответа")
        return _to("/clients")
    ctx.db.add(ClientAccess(user_id=ctx.user.id, reason=reason.strip()[:300]))
    nsvc.send(ctx.db, [u.id for u in _deciders(ctx)], "client_req", f"{ctx.user.full_name} просит доступ к клиенту",
              reason.strip()[:300], "/access-requests#clients", skip=ctx.user.id)
    ctx.db.commit()
    flash(ctx.request, "Запрос отправлен директору и заму — ответ придёт сообщением")
    return _to("/clients")


@router.post("/clients/{client_id}/grant")
def client_access_grant(client_id: str, ctx: Ctx = Depends(get_ctx), user_id: str = Form(""), hours: int = Form(0),
                        request_id: str = Form("")):
    """Директор / зам / админ дают менеджеру доступ к карточке клиента на срок (по запросу или сами)."""
    from datetime import timedelta
    from ..db import now
    from ..models import ClientAccess, Notification
    from ..models.orders import CLIENT_ACCESS_HOURS
    from ..services import notify as nsvc
    ctx.need("orders.grant_access")
    c = ctx.db.get(Client, _uuid(client_id))
    uid = svc._uuid(user_id)
    u = ctx.db.get(User, uid) if uid else None
    if not c or c.branch_id != ctx.branch.id or not u or u.branch_id != ctx.branch.id or hours not in CLIENT_ACCESS_HOURS:
        flash(ctx.request, "Выберите менеджера и срок", "error")
        return _to(f"/clients/{client_id}")
    req = ctx.db.get(ClientAccess, svc._uuid(request_id)) if svc._uuid(request_id) else None
    if req is None or req.status != "pending" or req.user_id != u.id:
        req = ctx.db.scalar(select(ClientAccess).where(ClientAccess.user_id == u.id, ClientAccess.status == "pending"))
    a = req or ClientAccess(user_id=u.id, reason="Дали без запроса")
    if req is None:
        ctx.db.add(a)
    a.client_id, a.status, a.hours, a.until = c.id, "approved", hours, now() + timedelta(hours=hours)
    a.decided_by, a.decided_at = ctx.user.id, now()
    until = a.until.astimezone().strftime("%d.%m %H:%M")
    nsvc.send(ctx.db, [u.id], "client_ok", f"Доступ к клиенту {c.full_name} — до {until}",
              f"Дал(а): {ctx.user.full_name} · на {CLIENT_ACCESS_HOURS[hours]}", f"/clients/{c.id}")
    for n in ctx.db.scalars(select(Notification).where(Notification.kind == "client_req", Notification.read_at.is_(None))):
        if not ctx.db.scalar(select(ClientAccess.id).where(ClientAccess.branch_id == ctx.branch.id, ClientAccess.status == "pending")):
            n.read_at = now()
    ctx.db.commit()
    flash(ctx.request, f"{u.full_name}: доступ к клиенту на {CLIENT_ACCESS_HOURS[hours]} — до {until}")
    return _to(f"/clients/{c.id}")


@router.post("/client-access/{aid}")
def client_access_decide(aid: str, ctx: Ctx = Depends(get_ctx), action: str = Form("")):
    """Отказать в запросе или закрыть доступ досрочно."""
    from ..db import now
    from ..models import ClientAccess
    from ..services import notify as nsvc
    ctx.need("orders.grant_access")
    a = ctx.db.get(ClientAccess, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id:
        raise Forbidden()
    if action == "decline" and a.status == "pending":
        a.status, a.decided_by, a.decided_at = "declined", ctx.user.id, now()
        nsvc.send(ctx.db, [a.user_id], "client_no", "Доступ к клиенту — отказано", f"Ответил(а): {ctx.user.full_name}", "/clients")
        flash(ctx.request, "Отказано")
    elif action == "close" and a.status == "approved":
        a.status, a.until = "closed", now()
        flash(ctx.request, "Доступ закрыт")
    ctx.db.commit()
    return _to("/access-requests#clients")


@router.post("/clients/{client_id}/regular")
def client_regular(client_id: str, ctx: Ctx = Depends(get_ctx)):
    """Отметить / снять «Постоянный клиент» вручную."""
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id or not svc.can_see_client(ctx, c.id):
        raise Forbidden()
    c.is_regular = not bool(c.is_regular)
    ctx.db.commit()
    flash(ctx.request, "Отмечен как постоянный" if c.is_regular else "Отметка «постоянный» снята")
    return RedirectResponse(f"/clients/{c.id}", status_code=303)


def _can_edit_clients(ctx: Ctx):
    ctx.need("clients.view")
    if not (ctx.can("orders.create") or ctx.user.role.code == "admin"):
        raise Forbidden()


@router.post("/clients/{client_id}/edit")
def client_edit(client_id: str, ctx: Ctx = Depends(get_ctx), full_name: str = Form(""), phone: str = Form("")):
    """Правка клиента (11.11.2026): имя и телефон. Телефон — один на клиента."""
    _can_edit_clients(ctx)
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id or not svc.can_see_client(ctx, c.id):
        raise Forbidden()
    ph = svc.norm_phone(phone)
    if not full_name.strip() or len(ph) < 10:
        flash(ctx.request, "Нужны имя и телефон", "error")
        return _to(f"/clients/{c.id}")
    other = ctx.db.scalar(select(Client).where(Client.branch_id == ctx.branch.id, Client.phone == ph, Client.id != c.id))
    if other:
        flash(ctx.request, f"Этот телефон уже у клиента «{other.full_name}»", "error")
        return _to(f"/clients/{c.id}")
    if c.phone != ph:
        svc.event(ctx.db, None, "note", f"Телефон изменён: {svc.fmt_phone(c.phone)} → {svc.fmt_phone(ph)}", client_id=c.id)
    if c.full_name != full_name.strip():
        svc.event(ctx.db, None, "note", f"Имя изменено: {c.full_name} → {full_name.strip()[:160]}", client_id=c.id)
    c.full_name, c.phone = full_name.strip()[:160], ph
    ctx.db.commit()
    flash(ctx.request, "Клиент сохранён")
    return _to(f"/clients/{c.id}")


def _vehicle(ctx: Ctx, vid: str) -> Vehicle:
    v = ctx.db.get(Vehicle, _uuid(vid))
    if not v or v.branch_id != ctx.branch.id or not svc.can_see_client(ctx, v.client_id):
        raise Forbidden()
    return v


@router.post("/vehicles/{vid}/edit")
def vehicle_edit(vid: str, ctx: Ctx = Depends(get_ctx), make: str = Form(""), model: str = Form(""), plate: str = Form(""),
                 year: str = Form(""), vin: str = Form(""), mileage: str = Form("")):
    """Правка машины (11.11.2026). Одинаковый VIN или госномер у другой машины — не даём (это та же машина)."""
    _can_edit_clients(ctx)
    v = _vehicle(ctx, vid)
    p, vn = svc.norm_plate(plate), vin.strip().upper().replace(" ", "")[:32]
    if not p or not make.strip() or not model.strip():
        flash(ctx.request, "Нужны марка, модель и госномер", "error")
        return _to(f"/clients/{v.client_id}")
    for field, val, label in ((Vehicle.plate, p, "госномер"), (Vehicle.vin, vn, "VIN")):
        if not val:
            continue
        dup = ctx.db.scalar(select(Vehicle).where(Vehicle.branch_id == ctx.branch.id, field == val, Vehicle.id != v.id))
        if dup:
            owner = ctx.db.get(Client, dup.client_id)
            flash(ctx.request, f"Такой {label} уже у машины {dup.make} {dup.model} ({owner.full_name if owner else ''}). "
                               f"Если это та же машина — передайте её через «Сменить владельца»", "error")
            return _to(f"/clients/{v.client_id}")
    if v.plate != p:
        svc.event(ctx.db, None, "note", f"Госномер изменён: {svc.fmt_plate(v.plate)} → {svc.fmt_plate(p)}",
                  client_id=v.client_id, vehicle_id=v.id)
    v.make, v.model, v.plate, v.vin = make.strip()[:60], model.strip()[:60], p, vn
    v.year = svc._int(year)
    m = svc._int(mileage)
    v.mileage = m if m is not None else v.mileage
    ctx.db.commit()
    flash(ctx.request, "Машина сохранена")
    return _to(f"/clients/{v.client_id}")


@router.post("/vehicles/{vid}/owner")
def vehicle_owner(vid: str, ctx: Ctx = Depends(get_ctx), phone: str = Form(""), name: str = Form("")):
    """Смена владельца: машина и её история ремонтов переходят к другому клиенту (есть в базе — по телефону, нет — новый)."""
    _can_edit_clients(ctx)
    v = _vehicle(ctx, vid)
    ph = svc.norm_phone(phone)
    if len(ph) < 10:
        flash(ctx.request, "Телефон нового владельца", "error")
        return _to(f"/clients/{v.client_id}")
    old = ctx.db.get(Client, v.client_id)
    new = ctx.db.scalar(select(Client).where(Client.branch_id == ctx.branch.id, Client.phone == ph))
    if new is None:
        if not name.strip():
            flash(ctx.request, "Такого телефона в базе нет — впишите имя нового владельца", "error")
            return _to(f"/clients/{v.client_id}")
        new = Client(full_name=name.strip()[:160], phone=ph)
        ctx.db.add(new)
        ctx.db.flush()
    if new.id == v.client_id:
        flash(ctx.request, "Это и есть текущий владелец", "error")
        return _to(f"/clients/{v.client_id}")
    car = f"{v.make} {v.model} {svc.fmt_plate(v.plate)}".strip()
    svc.event(ctx.db, None, "note", f"Автомобиль {car} передан новому владельцу: {new.full_name}", client_id=old.id, vehicle_id=v.id)
    v.client_id = new.id
    ctx.db.flush()
    svc.event(ctx.db, None, "note", f"Автомобиль {car} — от прошлого владельца: {old.full_name}. История ремонтов — в карточке",
              client_id=new.id, vehicle_id=v.id)
    ctx.db.commit()
    flash(ctx.request, f"Машина передана: {new.full_name}")
    return _to(f"/clients/{new.id}")


@router.get("/clients/{client_id}")
def client_view(client_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id or not svc.can_see_client(ctx, c.id):
        raise Forbidden()
    vehicles = ctx.db.scalars(select(Vehicle).where(Vehicle.client_id == c.id).order_by(Vehicle.created_at)).all()
    # Наряды клиента + история его машин при прошлых владельцах (смена владельца, 11.11.2026)
    # 12.11.2026: менеджер видит и прошлые визиты клиента у других менеджеров (вся история)
    orders = ctx.db.scalars(svc.client_orders_query(ctx, c.id, [v.id for v in vehicles])
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
                      can_edit=ctx.can("orders.create") or ctx.user.role.code == "admin",
                      grant=_grant_data(ctx, c) if ctx.can("orders.grant_access") else None,
                      lead=ctx.db.get(User, orders[0].manager_id) if orders and orders[0].manager_id else None)


# ======================= Доступ менеджера ко всем нарядам (12.11.2026) =======================
# Менеджер видит только свои наряды. Ко всем — по запросу с причиной: админ / директор / зам разрешают на 1, 2, 3 часа,
# день или неделю (или отказывают). Решает тот, кто ответил первым. Всё — в журнале.
def _to(url: str):
    return RedirectResponse(url, status_code=303)


def _deciders(ctx: Ctx) -> list:
    from ..permissions import effective_permissions
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))).all()
    return [u for u in staff if "orders.grant_access" in effective_permissions(ctx.db, u)]


@router.post("/orders/access-request")
def access_request(ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    from ..models import OrderAccess
    from ..services import notify as nsvc
    ctx.need("orders.view_all")
    if not reason.strip():
        flash(ctx.request, "Напишите причину — её увидит админ или директор", "error")
        return _to("/orders")
    if ctx.db.scalar(select(OrderAccess.id).where(OrderAccess.user_id == ctx.user.id, OrderAccess.status == "pending")):
        flash(ctx.request, "Запрос уже отправлен — ждём ответа")
        return _to("/orders")
    ctx.db.add(OrderAccess(user_id=ctx.user.id, reason=reason.strip()[:300]))
    nsvc.send(ctx.db, [u.id for u in _deciders(ctx)], "access_req", f"{ctx.user.full_name} просит доступ ко всем нарядам",
              reason.strip()[:300], "/access-requests", skip=ctx.user.id)
    ctx.db.commit()
    flash(ctx.request, "Запрос отправлен админу и директору — ответ придёт сообщением")
    return _to("/orders")


@router.get("/access-requests")
def access_requests(ctx: Ctx = Depends(get_ctx)):
    from ..models import OrderAccess
    from ..models.orders import ACCESS_HOURS, ACCESS_STATUS
    ctx.need("orders.grant_access")
    rows = ctx.db.scalars(select(OrderAccess).where(OrderAccess.branch_id == ctx.branch.id)
                          .order_by(OrderAccess.created_at.desc()).limit(60)).all()
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    from datetime import datetime, timezone
    now_ = datetime.now(timezone.utc)

    def active(a):
        return a.status == "approved" and a.until and (a.until if a.until.tzinfo else a.until.replace(tzinfo=timezone.utc)) > now_
    from ..models import OrderReopen
    reopens = ctx.db.scalars(select(OrderReopen).where(OrderReopen.branch_id == ctx.branch.id, OrderReopen.status == "pending")
                             .order_by(OrderReopen.created_at)).all()
    reopen_orders = {o.id: o for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.id.in_([r.work_order_id for r in reopens])))}
    return ctx.render("orders/access.html", title="Доступ ко всем нарядам", rows=rows, people=people, HOURS=ACCESS_HOURS,
                      reopens=reopens, reopen_orders=reopen_orders, **_client_access_lists(ctx),
                      STATUS=ACCESS_STATUS, pending=[a for a in rows if a.status == "pending"],
                      granted=[a for a in rows if active(a)], history=[a for a in rows if a.status != "pending" and not active(a)])


@router.post("/access-requests/{aid}/self")
def access_self(aid: str, ctx: Ctx = Depends(get_ctx)):
    """Директора и зама нет в здании (по табелю) — менеджер разрешает себе сам доступ ко всем нарядам, только на 1 час."""
    from datetime import timedelta
    from ..db import now
    from ..models import OrderAccess
    from ..services import decisions as dsvc
    a = ctx.db.get(OrderAccess, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id or a.user_id != ctx.user.id or a.status != "pending":
        raise Forbidden()
    if not dsvc.can_self_decide(ctx.db, ctx.branch.id):
        flash(ctx.request, "Директор или зам на месте (по табелю) — найдите их, решают они", "error")
        return _to("/orders")
    a.status, a.hours, a.until = "approved", dsvc.SELF_HOURS, now() + timedelta(hours=dsvc.SELF_HOURS)
    a.decided_by, a.decided_at, a.self_decided = ctx.user.id, now(), True
    dsvc.tell_deciders(ctx.db, ctx, "доступ ко всем нарядам на 1 час", f"Причина: {a.reason}", "/access-requests")
    ctx.db.commit()
    flash(ctx.request, "Доступ ко всем нарядам — на 1 час (директора и зама нет на месте). Им отправлено сообщение")
    return _to("/orders")


@router.post("/access-requests/{aid}")
def access_decide(aid: str, ctx: Ctx = Depends(get_ctx), action: str = Form(""), hours: int = Form(0)):
    from datetime import timedelta
    from ..db import now
    from ..models import Notification, OrderAccess
    from ..models.orders import ACCESS_HOURS
    from ..services import notify as nsvc
    ctx.need("orders.grant_access")
    a = ctx.db.get(OrderAccess, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id:
        raise Forbidden()
    who = ctx.db.get(User, a.user_id)
    if action == "close" and a.status == "approved":
        a.status, a.until = "closed", now()
        nsvc.send(ctx.db, [a.user_id], "access_no", "Доступ ко всем нарядам закрыт", f"Закрыл: {ctx.user.full_name}", "/orders")
        flash(ctx.request, f"{who.full_name if who else ''}: доступ закрыт")
    elif a.status != "pending":
        flash(ctx.request, "По этому запросу уже решили", "error")
    elif action == "approve" and hours in ACCESS_HOURS:
        a.status, a.hours, a.until = "approved", hours, now() + timedelta(hours=hours)
        a.decided_by, a.decided_at = ctx.user.id, now()
        until = a.until.astimezone().strftime("%d.%m %H:%M")
        nsvc.send(ctx.db, [a.user_id], "access_ok", f"Доступ ко всем нарядам — до {until}",
                  f"Разрешил: {ctx.user.full_name} · на {ACCESS_HOURS[hours]}", "/orders")
        flash(ctx.request, f"{who.full_name if who else ''}: разрешено на {ACCESS_HOURS[hours]} — до {until}")
    elif action == "decline":
        a.status, a.decided_by, a.decided_at = "declined", ctx.user.id, now()
        nsvc.send(ctx.db, [a.user_id], "access_no", "Доступ ко всем нарядам — отказано", f"Ответил: {ctx.user.full_name}", "/orders")
        flash(ctx.request, f"{who.full_name if who else ''}: отказано")
    # окошко «просит доступ» у остальных решающих больше не нужно
    for n in ctx.db.scalars(select(Notification).where(Notification.kind == "access_req", Notification.read_at.is_(None),
                                                       Notification.url == "/access-requests")):
        if not ctx.db.scalar(select(OrderAccess.id).where(OrderAccess.branch_id == ctx.branch.id, OrderAccess.status == "pending")):
            n.read_at = now()
    ctx.db.commit()
    return _to("/access-requests")
