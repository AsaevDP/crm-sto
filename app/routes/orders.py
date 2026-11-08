"""Наряды: список, приёмка (новый наряд), карточка, фото, клиенты."""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select

from ..models import Client, Lift, User, Vehicle, WoPhoto, WorkOrder, WoStatusHistory, WoWork
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, WO_ACTIVE, WO_STATUSES
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
    return [{
        "o": o, "v": vehicles.get(o.vehicle_id), "c": clients.get(o.client_id), "lift": lifts.get(o.lift_id),
        "mechanic": people.get(o.mechanic_id), "partsman": people.get(o.partsman_id),
        "photo": first_photo.get(o.id), "n_photos": n_photos.get(o.id, 0),
    } for o in orders]


# ---------- Список ----------
@router.get("/orders")
def orders_list(ctx: Ctx = Depends(get_ctx), tab: str = "active", q: str = ""):
    ctx.need("orders.view_all", "orders.view_own")
    query = svc.visible_orders_query(ctx)
    if tab == "active":
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
    return ctx.render("orders/list.html", title="Наряды", cards=_cards(ctx, orders), tab=tab, q=q,
                      STATUSES=WO_STATUSES, fmt_plate=svc.fmt_plate)


# ---------- Приёмка ----------
@router.get("/orders/new")
def order_new(ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.create")
    bid = ctx.branch.id
    return ctx.render("orders/new.html", title="Приёмка",
                      partsmen=svc.staff_by_role(ctx.db, bid, "partsman"),
                      mechanics=svc.staff_by_role(ctx.db, bid, "mechanic"),
                      lifts=svc.lifts_with_load(ctx.db, bid), makes=svc.known_makes(ctx.db, bid),
                      INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS, MAX_PHOTOS=svc.MAX_PHOTOS,
                      fmt_plate=svc.fmt_plate)


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
    people = _people(ctx, wo.manager_id, wo.partsman_id, wo.mechanic_id, *[h.created_by for h in history])
    can_assign = ctx.can("orders.assign") and wo.status in WO_ACTIVE
    return ctx.render(
        "orders/view.html", title=wo.number, wo=wo, v=db.get(Vehicle, wo.vehicle_id), c=db.get(Client, wo.client_id),
        lift=db.get(Lift, wo.lift_id) if wo.lift_id else None, photos=photos, works=works, history=history,
        people=people, STATUSES=WO_STATUSES, INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
        fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate, can_assign=can_assign,
        can_add_photo=(ctx.can("orders.create") or ctx.can("orders.diagnose")) and wo.status in WO_ACTIVE,
        can_cancel=ctx.can("orders.cancel") and wo.status in WO_ACTIVE,
        partsmen=svc.staff_by_role(db, ctx.branch.id, "partsman") if can_assign else [],
        mechanics=svc.staff_by_role(db, ctx.branch.id, "mechanic") if can_assign else [],
        lifts=svc.lifts_with_load(db, ctx.branch.id) if can_assign else [],
    )


@router.post("/orders/{order_id}/assign")
def order_assign(order_id: str, ctx: Ctx = Depends(get_ctx), partsman_id: str = Form(""),
                 mechanic_id: str = Form(""), lift_id: str = Form("")):
    ctx.need("orders.assign")
    wo = _order(ctx, order_id)
    bid = ctx.branch.id

    def pick(v, role):
        u = ctx.db.get(User, uuid.UUID(v)) if v else None
        return u.id if u and u.branch_id == bid and u.role.code == role else None

    wo.partsman_id, wo.mechanic_id = pick(partsman_id, "partsman"), pick(mechanic_id, "mechanic")
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
def clients_list(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("clients.view")
    if q.strip():
        items = svc.search_clients(ctx.db, ctx.branch.id, q, limit=50)
    else:
        items = svc.search_clients_recent(ctx.db, ctx.branch.id)
    return ctx.render("clients/list.html", title="Клиенты", items=items, q=q)


@router.get("/clients/{client_id}")
def client_view(client_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    vehicles = ctx.db.scalars(select(Vehicle).where(Vehicle.client_id == c.id).order_by(Vehicle.created_at)).all()
    orders = ctx.db.scalars(svc.visible_orders_query(ctx).where(WorkOrder.client_id == c.id)
                            .order_by(WorkOrder.created_at.desc())).all()
    return ctx.render("clients/view.html", title=c.full_name, c=c, vehicles=vehicles, cards=_cards(ctx, orders),
                      STATUSES=WO_STATUSES, fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate)
