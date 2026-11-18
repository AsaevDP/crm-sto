"""Мойка и детейлинг (12.11.2026, решение брата).

«Мойка» (менеджер, директор, зам — право wash.view): какие машины на мойке и по какому наряду, химия на складе,
кто сколько взял, «Закупка для мойки» (право wash.buy — заказать химию у поставщика, дальше как обычная закупка).
«Химия» (мойщик, детейлер — право chem.ask): что есть на складе, «Попросить» → кладовщик выдаёт.
Химия — расход СТО, входит в стоимость мойки, в счёт клиенту не идёт.
"""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_, select

from ..models import ChemRequest, Location, PartCatalog, Purchase, PurchaseLine, Supplier, User, Vehicle, WorkOrder, WoWork
from ..models.orders import WO_ACTIVE
from ..permissions import MASTERS, WASHERS
from ..db import now
from ..models.orders import FUEL_LEVELS
from ..models.wash import WashBox, WashService
from ..services import chem as csvc
from ..services import orders as osvc
from ..services import stock as ssvc
from ..services import wash as wsvc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()
LOW = Decimal(2)   # мало химии — «пора заказать»


def _to(url):
    return RedirectResponse(url, status_code=303)


def _dec(v, default="1") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", "").replace("\xa0", "").replace("₴", "").strip() or default)
    except InvalidOperation:
        return Decimal(default)


def _washer_ids(db, branch_id) -> list:
    return [u.id for u in osvc.staff_by_role(db, branch_id, list(WASHERS))]


def on_wash(db, branch_id) -> list[dict]:
    """Машины на мойке: открытые наряды, где есть работа мойщика / детейлера (или он ведущий мастер)."""
    ids = _washer_ids(db, branch_id)
    if not ids:
        return []
    orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE), or_(
        WorkOrder.mechanic_id.in_(ids),
        WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id.in_(ids)))))
        .order_by(osvc.FRESH)).all()   # 18.11.2026: свежие сверху
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == branch_id))}
    out = []
    for o in orders:
        works = [w for w in db.scalars(select(WoWork).where(WoWork.work_order_id == o.id).order_by(WoWork.created_at))
                 if (w.mechanic_id or o.mechanic_id) in ids]
        st = "done" if works and all(w.status == "done" for w in works) else (
            "doing" if any(w.status == "doing" for w in works) else ("paused" if any(w.status == "paused" for w in works) else "todo"))
        out.append({"o": o, "v": db.get(Vehicle, o.vehicle_id), "works": works, "st": st,
                    "who": [people[u] for u in dict.fromkeys(w.mechanic_id or o.mechanic_id for w in works) if u in people],
                    "mgr": people.get(o.manager_id)})
    return out


WASH_ST = {"todo": ("ждёт мойки", "created"), "doing": ("моют", "work"), "paused": ("пауза", "waiting"), "done": ("готово", "ready")}


@router.get("/wash")
def wash(ctx: Ctx = Depends(get_ctx), tab: str = ""):
    ctx.need("wash.view")
    db, bid = ctx.db, ctx.branch.id
    # 18.11.2026: «С ремонта» — мойка как работа в обычном наряде; быстрые наряды мойки — в очереди
    cars = [x for x in on_wash(db, bid) if not wsvc.is_wash_order(x["o"])]
    brd = wsvc.board(db, bid)
    items = csvc.items(db, bid)
    asks = csvc.requests(db, bid, "new")
    chem_p = db.scalars(select(Purchase).where(Purchase.branch_id == bid, Purchase.kind == "chem")
                        .order_by(Purchase.created_at.desc()).limit(100)).all()
    if tab not in ("now", "chem", "buy", "asks"):
        tab = "now"
    low = [x for x in items if x["free"] <= LOW]
    data = dict(tab=tab, cars=cars, items=items, asks=asks, low=low, WASH_ST=WASH_ST, fmt_plate=osvc.fmt_plate,
                brd=brd, fmt_min=wsvc.fmt_min, CLASSES=wsvc.CLASSES, now=now(),
                n_open=sum(1 for p in chem_p if p.status != "arrived"),
                supplier_list=db.scalars(select(Supplier).where(Supplier.branch_id == bid).order_by(Supplier.name)).all())
    if tab == "chem":
        data.update(usage=csvc.usage_by_person(db, bid, 30), done=csvc.requests(db, bid, ("issued", "declined"), days=30)[:40],
                    locs={l.id: ssvc.location_label(l) for l in db.scalars(select(Location).where(Location.branch_id == bid))},
                    cost_ok=ctx.can("prices.view_purchase"))
    if tab == "asks":   # 18.11.2026: плитка «Просят» — кто и какую химию просит, для какой машины
        data.update(ask_cars={v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_(
                        [x["wo"].vehicle_id for x in asks if x["wo"]])))} if asks else {},
                    free={x["c"].id: x["free"] for x in items},
                    locs={l.id: ssvc.location_label(l) for l in db.scalars(select(Location).where(Location.branch_id == bid))},
                    item_loc={x["c"].id: x["loc"] for x in items})
    if tab == "buy":
        stats = {}
        for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id.in_([p.id for p in chem_p]))):
            s = stats.setdefault(l.purchase_id, {"n": 0, "total": Decimal(0)})
            s["n"] += 1
            s["total"] += l.qty * l.price
        data.update(purchases=chem_p, stats=stats,
                    suppliers={s.id: s for s in data["supplier_list"]},
                    people={u.id: u for u in db.scalars(select(User).where(User.branch_id == bid))})
    return ctx.render("wash/index.html", title="Мойка", **data)


@router.post("/wash/purchases/new")
def wash_purchase_new(ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form("")):
    """Новая закупка химии: черновик, дальше — обычная закупка (строки, «✓ Заказал», кладовщик принимает)."""
    ctx.need("wash.buy")
    sid = None
    if supplier_new.strip():
        s = Supplier(name=supplier_new.strip()[:160])
        ctx.db.add(s)
        ctx.db.flush()
        sid = s.id
    elif supplier_id:
        s = ctx.db.get(Supplier, _uuid(supplier_id))
        sid = s.id if s and s.branch_id == ctx.branch.id else None
    p = Purchase(status="draft", supplier_id=sid, kind="chem")
    ctx.db.add(p)
    ctx.db.commit()
    flash(ctx.request, "Закупка химии создана — добавьте строки и нажмите «✓ Заказал»")
    return _to(f"/purchases/{p.id}#lines")


# ---------- Мойщик / детейлер ----------
def _my_orders(ctx: Ctx) -> list[WorkOrder]:
    me = ctx.user.id
    return ctx.db.scalars(select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, WorkOrder.status.in_(WO_ACTIVE), or_(
        WorkOrder.mechanic_id == me, WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me))))
        .order_by(WorkOrder.created_at)).all()


@router.get("/wash/my")
def wash_my(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("chem.ask")
    db, bid = ctx.db, ctx.branch.id
    items = csvc.items(db, bid)
    if q.strip():
        ql = q.strip().lower()
        items = [x for x in items if ql in (x["c"].name or "").lower() or ql in (x["c"].brand or "").lower()]
    orders = _my_orders(ctx)
    return ctx.render("wash/my.html", title="Химия", items=items, q=q, orders=orders,
                      vehicles={v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))},
                      mine=csvc.requests(db, bid, who=ctx.user.id, days=14), STATUS=csvc.STATUS, fmt_plate=osvc.fmt_plate,
                      locs={l.id: ssvc.location_label(l) for l in db.scalars(select(Location).where(Location.branch_id == bid))})


@router.post("/wash/ask")
def wash_ask(ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(...), qty: str = Form("1"), order_id: str = Form(""),
             note: str = Form("")):
    ctx.need("chem.ask")
    c = ctx.db.get(PartCatalog, _uuid(catalog_id))
    if not c or c.section != "chem":
        raise Forbidden()
    n = max(_dec(qty), Decimal("0.01"))
    wo = ctx.db.get(WorkOrder, _uuid(order_id)) if order_id else None
    if wo and (wo.branch_id != ctx.branch.id or wo.id not in {o.id for o in _my_orders(ctx)}):
        wo = None
    csvc.ask(ctx.db, ctx.user, c.id, n, wo.id if wo else None, note)
    ctx.db.commit()
    flash(ctx.request, f"Запрос отправлен кладовщику: {c.name} · {n.normalize():f} шт")
    return _to("/wash/my")


@router.post("/wash/requests/{rid}/cancel")
def wash_cancel(rid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("chem.ask")
    r = ctx.db.get(ChemRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id or r.created_by != ctx.user.id or r.status != "new":
        raise Forbidden()
    r.status = "cancelled"
    ctx.db.commit()
    flash(ctx.request, "Запрос отменён")
    return _to("/wash/my")


# ---------- Кладовщик ----------
@router.post("/chem/{rid}/issue")
def chem_issue(rid: str, ctx: Ctx = Depends(get_ctx), qty: str = Form("")):
    ctx.need("stock.issue")
    r = ctx.db.get(ChemRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id or r.status != "new":
        raise Forbidden()
    err = csvc.issue(ctx.db, r, ctx.user, _dec(qty, str(r.qty)) if qty else None)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Химия выдана — списана со склада как расход мойки")
    return _to("/stock/issues?tab=chem")


@router.post("/chem/{rid}/decline")
def chem_decline(rid: str, ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    ctx.need("stock.issue")
    r = ctx.db.get(ChemRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id or r.status != "new":
        raise Forbidden()
    csvc.decline(ctx.db, r, ctx.user, reason)
    ctx.db.commit()
    flash(ctx.request, "Отказано — мойщик получил сообщение")
    return _to("/stock/issues?tab=chem")


# ======================= Быстрая приёмка на мойку (18.11.2026, брат) =======================
@router.get("/wash/new")
def wash_new(ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.create")
    db, bid = ctx.db, ctx.branch.id
    import uuid as _u
    return ctx.render("wash/new.html", title="Приёмка на мойку", services=wsvc.services(db, bid, active_only=True),
                      boxes=wsvc.boxes(db, bid), washers=osvc.staff_by_role(db, bid, list(WASHERS)), MASTERS=MASTERS,
                      CLASSES=wsvc.CLASSES, KINDS=wsvc.KINDS, FUEL_LEVELS=FUEL_LEVELS, makes=osvc.known_makes(db, bid),
                      brd=wsvc.board(db, bid), fmt_min=wsvc.fmt_min, intake_token=_u.uuid4().hex)


@router.post("/wash/new")
async def wash_create(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Форма приёмки на мойку: клиент, машина, класс, услуги из прайса (= работы с ценой), кто моет, бокс, топливо.
    Фото — по желанию. Наряд сразу «В работе» и встаёт в очередь мойки."""
    ctx.need("orders.create")
    db, bid = ctx.db, ctx.branch.id
    form = await request.form(max_files=2 * osvc.MAX_PHOTOS + 2)
    g = lambda k: str(form.get(k, "") or "")
    token = g("intake_token").strip()[:40] or None
    if token:
        same = db.scalar(select(WorkOrder).where(WorkOrder.intake_token == token))
        if same:
            return {"ok": True, "url": f"/orders/{same.id}"}
    car_class = g("car_class") if g("car_class") in wsvc.CLASSES else ""
    ids = [_uuid_or_none(x) for x in form.getlist("service_id")]
    chosen = [s for s in wsvc.services(db, bid, active_only=True) if s.id in ids]
    washer = db.get(User, _uuid_or_none(g("washer_id"))) if _uuid_or_none(g("washer_id")) else None
    if washer and (washer.branch_id != bid or not washer.active or washer.role.code not in WASHERS):
        washer = None
    box = db.get(WashBox, _uuid_or_none(g("box_id"))) if _uuid_or_none(g("box_id")) else None
    if box and (box.branch_id != bid or not box.active):
        box = None
    f = osvc.IntakeForm(client_id=g("client_id"), vehicle_id=g("vehicle_id"), phone=g("phone"), name=g("name"),
                        plate=g("plate"), make=g("make"), model=g("model"), vin=g("vin"), mileage=g("mileage"),
                        scenario="A", complaint=g("note").strip()[:1000], mechanic_id=str(washer.id) if washer else "",
                        fuel_level=g("fuel_level"), intake_note=g("intake_note"))
    errors = []
    if len(osvc.norm_phone(f.phone)) < 10:
        errors.append("Телефон клиента")
    if not f.name.strip():
        errors.append("Имя клиента")
    if not osvc.norm_plate(f.plate):
        errors.append("Госномер")
    if not f.make.strip() or not f.model.strip():
        errors.append("Марка и модель")
    if not car_class:
        errors.append("Класс машины")
    if not chosen:
        errors.append("Хотя бы одна услуга")
    errors = errors or f.problems(db, bid)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    wo = osvc.create_order(db, ctx, f)
    wo.intake_token = token
    wo.kind, wo.car_class, wo.wash_box_id = "wash", car_class, box.id if box else None
    for s in chosen:
        p = wsvc.price(s, car_class)
        db.add(WoWork(work_order_id=wo.id, name=s.name[:200], pricing="fixed", rate=p, qty=Decimal(1), price=p,
                      mechanic_id=washer.id if washer else None, wash_service_id=s.id, minutes=s.minutes))
    photos = [p for p in form.getlist("photos") if hasattr(p, "read")][:osvc.MAX_PHOTOS]
    thumbs = [p for p in form.getlist("thumbs") if hasattr(p, "read")]
    for i, p in enumerate(photos):
        thumb = await thumbs[i].read() if i < len(thumbs) else None
        osvc.save_photo(db, wo, await p.read(), thumb)
    names = ", ".join(s.name for s in chosen)
    osvc.set_status(db, wo, "work", f"Мойка: {names}" + (f" · бокс {box.name or box.number}" if box else ""))
    from sqlalchemy.exc import IntegrityError
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        same = db.scalar(select(WorkOrder).where(WorkOrder.intake_token == token)) if token else None
        if same:
            return {"ok": True, "url": f"/orders/{same.id}"}
        raise
    pos = next((c.pos for c in wsvc.board(db, bid)["queue"] if c.o.id == wo.id), 0)
    flash(request, f"Наряд {wo.number} — на мойку" + (f", в очереди {pos}-й" if pos else ""))
    return {"ok": True, "url": "/wash?tab=now"}


def _uuid_or_none(v):
    import uuid as _u
    try:
        return _u.UUID(str(v)) if v else None
    except ValueError:
        return None


@router.post("/wash/{order_id}/up")
def wash_up(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """«↑» — поднять машину в очереди (менеджер)."""
    ctx.need("orders.approve")
    wo = ctx.db.get(WorkOrder, _uuid(order_id))
    if not wo or wo.branch_id != ctx.branch.id or not wsvc.is_wash_order(wo):
        raise Forbidden()
    if wsvc.raise_up(ctx.db, ctx.branch.id, wo):
        osvc.event(ctx.db, wo, "status", "Мойка: машину подняли в очереди")
        ctx.db.commit()
    return _to("/wash?tab=now#queue")


# ---------- Очередь у мойщика ----------
@router.get("/wash/queue")
def wash_queue(ctx: Ctx = Depends(get_ctx)):
    """Экран мойщика / детейлера: моя машина сейчас, очередь с «Взять следующую», готовые; машины с ремонта."""
    ctx.need("chem.ask", "wash.view")
    db, bid, me = ctx.db, ctx.branch.id, ctx.user
    brd = wsvc.board(db, bid)
    mine_now = [c for c in brd["doing"] if any(u.id == me.id for u in c.who)]
    nxt = wsvc.next_for(db, bid, me)
    repair = [x for x in on_wash(db, bid) if not wsvc.is_wash_order(x["o"]) and x["st"] != "done"
              and any(u.id == me.id for u in x["who"])]
    return ctx.render("wash/queue.html", title="Мойка · очередь", brd=brd, mine_now=mine_now, nxt=nxt, repair=repair,
                      can_take=lambda c: wsvc.can_take(me, c), fmt_min=wsvc.fmt_min, fmt_plate=osvc.fmt_plate,
                      CLASSES=wsvc.CLASSES, now=now(), WASH_ST=WASH_ST)


@router.post("/wash/take")
def wash_take(ctx: Ctx = Depends(get_ctx), order_id: str = Form("")):
    """«Взять следующую» (или конкретную машину из очереди) — мойщик становится исполнителем, первая услуга «Начал»."""
    ctx.need("chem.ask")
    if ctx.user.role.code not in WASHERS:
        raise Forbidden()
    db, bid = ctx.db, ctx.branch.id
    if order_id:
        c = next((c for c in wsvc.board(db, bid)["queue"] if str(c.o.id) == order_id), None)
        wo = c.o if c and wsvc.can_take(ctx.user, c) else None
    else:
        wo = wsvc.next_for(db, bid, ctx.user)
    if not wo:
        flash(ctx.request, "Очередь пуста — или машину уже взял другой мойщик", "error")
        return _to("/wash/queue")
    wsvc.take(db, wo, ctx.user)
    db.commit()
    flash(ctx.request, f"Взяли: наряд {wo.number}. Отмечайте «Выполнено» по каждой услуге")
    return _to(f"/orders/{wo.id}")


# ======================= Прайс мойки (18.11.2026) =======================
def _money(v) -> Decimal:
    d = _dec(v, "0")
    return max(d.quantize(Decimal("0.01")), Decimal(0))


@router.get("/wash/prices")
def wash_prices(ctx: Ctx = Depends(get_ctx), hidden: str = ""):
    ctx.need("wash.view", "chem.ask", "wash.prices")
    rows = wsvc.services(ctx.db, ctx.branch.id)
    ctx.db.commit()   # прайс по умолчанию заводится при первом открытии
    return ctx.render("wash/prices.html", title="Прайс мойки", rows=rows, can_edit=ctx.can("wash.prices"),
                      CLASSES=wsvc.CLASSES, KINDS=wsvc.KINDS, fmt_min=wsvc.fmt_min, hidden=hidden == "1",
                      n_hidden=sum(1 for r in rows if not r.active))


@router.post("/wash/prices")
def wash_price_add(ctx: Ctx = Depends(get_ctx), name: str = Form(...), kind: str = Form("wash"), price_car: str = Form("0"),
                   price_suv: str = Form(""), price_bus: str = Form(""), minutes: str = Form("30")):
    """«＋ Добавить услугу» — менеджер, директор, зам, админ."""
    ctx.need("wash.prices")
    name = name.strip()[:120]
    if not name:
        flash(ctx.request, "Впишите название услуги", "error")
        return _to("/wash/prices")
    db, bid = ctx.db, ctx.branch.id
    if db.scalar(select(WashService.id).where(WashService.branch_id == bid, WashService.name == name)):
        flash(ctx.request, f"Услуга «{name}» уже есть в прайсе", "error")
        return _to("/wash/prices")
    car = _money(price_car)
    last = max([s.sort for s in wsvc.services(db, bid)] or [0])
    s = WashService(name=name, kind=kind if kind in wsvc.KINDS else "wash", price_car=car,
                    price_suv=_money(price_suv) if price_suv.strip() else car,
                    price_bus=_money(price_bus) if price_bus.strip() else car,
                    minutes=max(int(_dec(minutes, "30")), 5), sort=last + 10)
    db.add(s)
    db.commit()
    flash(ctx.request, f"Услуга «{name}» добавлена в прайс")
    return _to(f"/wash/prices#s{s.id}")


@router.post("/wash/prices/{sid}")
def wash_price_save(sid: str, ctx: Ctx = Depends(get_ctx), name: str = Form(""), kind: str = Form("wash"),
                    price_car: str = Form("0"), price_suv: str = Form("0"), price_bus: str = Form("0"),
                    minutes: str = Form("30"), active: str = Form(None), toggle: str = Form("")):
    """Правка цены / названия / длительности; «Скрыть» / «Вернуть» — услуга пропадает из выбора, старые наряды не меняются.
    В журнале — кто, когда, было → стало (журнал пишется сам)."""
    ctx.need("wash.prices")
    s = ctx.db.get(WashService, _uuid(sid))
    if not s or s.branch_id != ctx.branch.id:
        raise Forbidden()
    if toggle:
        s.active = not s.active
        ctx.db.commit()
        flash(ctx.request, f"«{s.name}» " + ("снова в прайсе" if s.active else "скрыта — при приёмке не предлагается"))
        return _to("/wash/prices" + ("" if s.active else "?hidden=1") + f"#s{s.id}")
    s.name = name.strip()[:120] or s.name
    s.kind = kind if kind in wsvc.KINDS else s.kind
    s.price_car, s.price_suv, s.price_bus = _money(price_car), _money(price_suv), _money(price_bus)
    s.minutes = max(int(_dec(minutes, str(s.minutes or 30))), 5)
    ctx.db.commit()
    if ctx.request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    flash(ctx.request, f"«{s.name}» сохранена")
    return _to(f"/wash/prices#s{s.id}")


# ======================= Боксы мойки (18.11.2026) =======================
@router.get("/wash/boxes")
def wash_boxes(ctx: Ctx = Depends(get_ctx)):
    ctx.need("wash.prices")
    return ctx.render("wash/boxes.html", title="Боксы мойки", boxes=wsvc.boxes(ctx.db, ctx.branch.id, active_only=False),
                      brd=wsvc.board(ctx.db, ctx.branch.id), fmt_plate=osvc.fmt_plate)


@router.post("/wash/boxes")
def wash_box_add(ctx: Ctx = Depends(get_ctx), name: str = Form("")):
    ctx.need("wash.prices")
    from sqlalchemy import func
    n = (ctx.db.scalar(select(func.max(WashBox.number)).where(WashBox.branch_id == ctx.branch.id)) or 0) + 1
    ctx.db.add(WashBox(number=n, name=name.strip()[:60] or f"Мойка {n}"))
    ctx.db.commit()
    flash(ctx.request, f"Добавлен бокс {n}")
    return _to("/wash/boxes")


@router.post("/wash/boxes/{bid_}")
def wash_box_save(bid_: str, ctx: Ctx = Depends(get_ctx), name: str = Form(""), active: str = Form("")):
    ctx.need("wash.prices")
    b = ctx.db.get(WashBox, _uuid(bid_))
    if not b or b.branch_id != ctx.branch.id:
        raise Forbidden()
    b.name = name.strip()[:60] or f"Мойка {b.number}"
    b.active = bool(active)
    ctx.db.commit()
    flash(ctx.request, f"Бокс {b.number} сохранён")
    return _to("/wash/boxes")


# ======================= Наряд глазами мойщика (18.11.2026, брат) =======================
def washer_order_view(ctx: Ctx, wo: WorkOrder):
    """Мойщику и детейлеру — только нужное: машина, фото при приёмке, ЕГО услуги с «Начал / Пауза / Выполнено»,
    сообщения по машине, химия. Без этапов ремонта, проблем, диагностики, согласования, запчастей, цен и денег.
    Машина с ремонта (мойка — одна из работ большого наряда) — видна только своя работа."""
    from ..models import Client, WoPhoto, WoWorkSession
    from .orders import _order_msgs
    db, me = ctx.db, ctx.user
    works_all = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all()
    is_wash = wsvc.is_wash_order(wo)
    unassigned = is_wash and not wo.mechanic_id and not any(w.mechanic_id for w in works_all)
    mine = [w for w in works_all if osvc.work_owner(wo, w) == me.id or (unassigned and not w.mechanic_id)]
    others = [w for w in works_all if w not in mine] if is_wash else []
    photos = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id, WoPhoto.finding_id.is_(None),
                                              WoPhoto.stage == "intake").order_by(WoPhoto.created_at)).all()
    pauses = {s.wo_work_id: s for s in db.scalars(select(WoWorkSession).where(
        WoWorkSession.work_order_id == wo.id, WoWorkSession.kind == "pause", WoWorkSession.ended_at.is_(None)))}
    take_c = None
    if is_wash and wo.status == "work":
        take_c = next((c for c in wsvc.board(db, ctx.branch.id)["queue"] if c.o.id == wo.id and wsvc.can_take(me, c)), None)
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    box = db.get(WashBox, wo.wash_box_id) if wo.wash_box_id else None
    c = db.get(Client, wo.client_id)
    return ctx.render("wash/order.html", title=wo.number, wo=wo, v=db.get(Vehicle, wo.vehicle_id), client=c,
                      works=mine, others=others, photos=photos, pauses=pauses, is_wash=is_wash, take_c=take_c,
                      own_ok={w.id for w in mine if osvc.work_owner(wo, w) == me.id and osvc.can_start_work(db, wo, w)},
                      people=people, box=box, CLASSES=wsvc.CLASSES, fmt_plate=osvc.fmt_plate, fmt_min=wsvc.fmt_min,
                      order_msgs=_order_msgs(ctx, wo), FUEL_LEVELS=FUEL_LEVELS)
