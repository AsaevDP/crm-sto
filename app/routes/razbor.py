"""Шаг 7: разбор — доноры, б/у детали, продажа (резерв → продано), экспорт для сайта."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select

from ..models import Donor, DonorPhoto, Location, PartCatalog, StockItem, StockMovement, StockPhoto, UsedSale, User
from ..models.money import PAY_METHODS
from ..models.razbor import CONDITIONS, DELIVERY, DONOR_STATUS, SALE_STATUS
from ..models.stock import MOVEMENT_TYPES
from ..services import orders as osvc
from ..services import razbor as svc
from ..services import stock as ssvc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return Decimal(default)


def _int(v):
    try:
        return int(str(v).replace(" ", "")) if str(v or "").strip() else None
    except ValueError:
        return None


def _to(url):
    return RedirectResponse(url, status_code=303)


def _locations(ctx: Ctx):
    return ctx.db.scalars(select(Location).where(Location.branch_id == ctx.branch.id).order_by(Location.rack, Location.shelf, Location.place)).all()


async def _photos(form, field="photos") -> list[str]:
    out = []
    for f in form.getlist(field):
        if hasattr(f, "read"):
            data = await f.read()
            if data and (p := svc.save_photo(data)):
                out.append(p)
    return out


def _item_rows(ctx: Ctx, items):
    db = ctx.db
    bal = ssvc.balances(db, ctx.branch.id, [i.id for i in items])
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([i.catalog_id for i in items])))}
    donors = {d.id: d for d in db.scalars(select(Donor).where(Donor.branch_id == ctx.branch.id))}
    locs = {l.id: l for l in _locations(ctx)}
    first = {}
    for p in db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id.in_([i.id for i in items])).order_by(StockPhoto.created_at)):
        first.setdefault(p.stock_item_id, p)
    rows = []
    for i in items:
        b = bal[i.id]
        state = "sold" if b["on_hand"] <= 0 else ("reserved" if b["free"] <= 0 else "stock")
        rows.append({"i": i, "c": cats.get(i.catalog_id), "d": donors.get(i.donor_id), "b": b, "state": state,
                     "loc": ssvc.location_label(locs.get(i.location_id)), "photo": first.get(i.id)})
    return rows


# ======================= Список =======================
@router.get("/razbor")
def razbor(ctx: Ctx = Depends(get_ctx), q: str = "", f: str = "", donor: str = ""):
    ctx.need("razbor.view")
    db = ctx.db
    query = select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == ctx.branch.id, PartCatalog.kind == "used")
    if donor:
        query = query.where(StockItem.donor_id == _uuid(donor))
    rows = _item_rows(ctx, db.scalars(query.order_by(StockItem.code.desc())).all())
    if q.strip():
        ql = q.strip().lower()
        rows = [r for r in rows if ql in f"{r['i'].code} {r['c'].name} {r['i'].donor_make} {r['i'].donor_model} {r['i'].donor_vin}".lower()]
    if f == "site":
        rows = [r for r in rows if r["i"].publish_on_site and r["state"] == "stock"]
    elif f in ("stock", "reserved", "sold"):
        rows = [r for r in rows if r["state"] == f]
    donors = db.scalars(select(Donor).where(Donor.branch_id == ctx.branch.id).order_by(Donor.created_at.desc())).all()
    eco = {d.id: svc.economy(db, ctx.branch.id, d) for d in donors}
    reserved = db.scalars(select(UsedSale).where(UsedSale.branch_id == ctx.branch.id, UsedSale.status == "reserved")).all()
    return ctx.render("razbor/list.html", title="Разбор", rows=rows, q=q, f=f, donor=donor, donors=donors, eco=eco,
                      n_reserved=len(reserved), CONDITIONS=CONDITIONS, DONOR_STATUS=DONOR_STATUS,
                      n_site=len(svc.export_rows(db, ctx.branch.id)) if ctx.can("razbor.export") else 0)


# ======================= Донор =======================
@router.post("/razbor/donors")
async def donor_add(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.manage")
    form = await request.form(max_files=30)
    make, model = str(form.get("make", "")).strip(), str(form.get("model", "")).strip()
    if not make or not model:
        flash(request, "Марка и модель обязательны", "error")
        return _to("/razbor")
    d = Donor(code=svc.next_donor_code(ctx.db, ctx.branch.id), make=make[:60], model=model[:60], year=_int(form.get("year")),
              vin=str(form.get("vin", "")).strip().upper()[:32], mileage=_int(form.get("mileage")),
              purchase_price=_dec(form.get("purchase_price")), bought_on=date.today())
    ctx.db.add(d)
    ctx.db.flush()
    for p in await _photos(form):
        ctx.db.add(DonorPhoto(donor_id=d.id, file=p))
    ctx.db.commit()
    flash(request, f"Донор {d.code} заведён — добавляйте детали")
    return _to(f"/razbor/donors/{d.id}")


def _donor(ctx: Ctx, did: str) -> Donor:
    d = ctx.db.get(Donor, _uuid(did))
    if not d or d.branch_id != ctx.branch.id:
        raise Forbidden()
    return d


@router.get("/razbor/donors/{did}")
def donor_view(did: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.view")
    d = _donor(ctx, did)
    db = ctx.db
    rows = _item_rows(ctx, db.scalars(select(StockItem).where(StockItem.donor_id == d.id).order_by(StockItem.code)).all())
    photos = db.scalars(select(DonorPhoto).where(DonorPhoto.donor_id == d.id).order_by(DonorPhoto.created_at)).all()
    return ctx.render("razbor/donor.html", title=f"{d.code} {d.make} {d.model}", d=d, rows=rows, photos=photos,
                      e=svc.economy(db, ctx.branch.id, d), locations=_locations(ctx), CONDITIONS=CONDITIONS,
                      DONOR_STATUS=DONOR_STATUS, loc_label=ssvc.location_label)


@router.post("/razbor/donors/{did}")
async def donor_save(did: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.manage")
    d = _donor(ctx, did)
    form = await request.form(max_files=30)
    for k in ("make", "model", "vin", "engine", "gearbox", "body", "color", "expenses_note", "notes"):
        if k in form:
            setattr(d, k, str(form.get(k, "")).strip()[:200 if k not in ("expenses_note", "notes") else 2000])
    d.vin = d.vin.upper()
    d.year, d.mileage = _int(form.get("year")), _int(form.get("mileage"))
    d.purchase_price, d.expenses = _dec(form.get("purchase_price")), _dec(form.get("expenses"))
    if form.get("status") in DONOR_STATUS:
        d.status = str(form.get("status"))
    try:
        d.bought_on = date.fromisoformat(str(form.get("bought_on"))) if form.get("bought_on") else d.bought_on
    except ValueError:
        pass
    for p in await _photos(form):
        ctx.db.add(DonorPhoto(donor_id=d.id, file=p))
    ctx.db.commit()
    flash(request, "Сохранено")
    return _to(f"/razbor/donors/{did}")


@router.post("/razbor/donors/{did}/parts")
async def part_add(did: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Деталь с донора: название, состояние, цена, ячейка, фото, «на сайт». Код Р-0001 — сам."""
    ctx.need("razbor.manage")
    d = _donor(ctx, did)
    form = await request.form(max_files=30)
    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "Название детали обязательно", "error")
        return _to(f"/razbor/donors/{did}")
    loc = ctx.db.get(Location, _uuid(str(form.get("location_id")))) if form.get("location_id") else None
    item = svc.add_part(ctx.db, ctx.branch.id, d, name, _dec(form.get("qty"), "1"), _dec(form.get("price")),
                        str(form.get("condition", "good")), loc.id if loc else None, str(form.get("description", "")),
                        publish=form.get("publish") == "1", article=str(form.get("article", "")))
    for p in await _photos(form):
        ctx.db.add(StockPhoto(stock_item_id=item.id, file=p))
    ctx.db.commit()
    flash(request, f"Деталь {item.code} добавлена — подпишите бирку")
    return _to(f"/razbor/donors/{did}")


# ======================= Деталь =======================
def _item(ctx: Ctx, iid: str) -> StockItem:
    i = ctx.db.get(StockItem, _uuid(iid))
    if not i or i.branch_id != ctx.branch.id or not i.code:
        raise Forbidden()
    return i


@router.get("/razbor/parts/{iid}")
def part_view(iid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.view")
    i = _item(ctx, iid)
    db = ctx.db
    row = _item_rows(ctx, [i])[0]
    photos = db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id == i.id).order_by(StockPhoto.created_at)).all()
    sales = db.scalars(select(UsedSale).where(UsedSale.stock_item_id == i.id).order_by(UsedSale.created_at.desc())).all()
    moves = db.scalars(select(StockMovement).where(StockMovement.stock_item_id == i.id).order_by(StockMovement.created_at.desc())).all()
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("razbor/part.html", title=f"{i.code} {row['c'].name}", r=row, i=i, photos=photos, sales=sales,
                      moves=moves, people=people, locations=_locations(ctx), CONDITIONS=CONDITIONS, DELIVERY=DELIVERY,
                      SALE_STATUS=SALE_STATUS, PAY_METHODS=PAY_METHODS, MOVEMENT_TYPES=MOVEMENT_TYPES,
                      loc_label=ssvc.location_label, fmt_phone=osvc.fmt_phone)


@router.post("/razbor/parts/{iid}")
async def part_save(iid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.manage")
    i = _item(ctx, iid)
    form = await request.form(max_files=30)
    cat = ctx.db.get(PartCatalog, i.catalog_id)
    cat.name = str(form.get("name", cat.name)).strip()[:200] or cat.name
    i.sale_price = ssvc.money(_dec(form.get("price")))
    if form.get("condition") in CONDITIONS:
        i.condition = str(form.get("condition"))
    i.description = str(form.get("description", "")).strip()
    i.publish_on_site = form.get("publish") == "1"
    loc = ctx.db.get(Location, _uuid(str(form.get("location_id")))) if form.get("location_id") else None
    i.location_id = loc.id if loc else None
    for p in await _photos(form):
        ctx.db.add(StockPhoto(stock_item_id=i.id, file=p))
    ctx.db.commit()
    flash(request, "Сохранено")
    return _to(f"/razbor/parts/{iid}")


@router.post("/razbor/parts/{iid}/sell")
async def part_sell(iid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Резерв под покупателя или сразу «Продано»."""
    ctx.need("razbor.sell")
    i = _item(ctx, iid)
    form = await request.form()
    action = str(form.get("action", "reserve"))
    args = dict(qty=_dec(form.get("qty"), "1"), price=_dec(form.get("price")), buyer_name=str(form.get("buyer_name", "")),
                buyer_phone=str(form.get("buyer_phone", "")),
                delivery=str(form.get("delivery", "pickup")) if form.get("delivery") in DELIVERY else "pickup",
                pay_method=str(form.get("pay_method", "cash")) if form.get("pay_method") in PAY_METHODS else "cash")
    if action == "sold":
        res = svc.sell_now(ctx.db, ctx.branch.id, i, ttn=str(form.get("ttn", "")), **args)
    else:
        res = svc.reserve_for_buyer(ctx.db, ctx.branch.id, i, comment=str(form.get("comment", "")), **args)
    if isinstance(res, str):
        flash(request, res, "error")
    else:
        ctx.db.commit()
        flash(request, "Продано" if action == "sold" else "Деталь в резерве под покупателя")
    return _to(f"/razbor/parts/{iid}")


@router.post("/razbor/sales/{sid}")
async def sale_update(sid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.sell")
    s = ctx.db.get(UsedSale, _uuid(sid))
    if not s or s.branch_id != ctx.branch.id:
        raise Forbidden()
    form = await request.form()
    if form.get("action") == "cancel":
        svc.cancel_reserve(ctx.db, s)
        flash(request, "Резерв отменён — деталь снова свободна")
    elif s.status == "reserved":
        pm = str(form.get("pay_method")) if form.get("pay_method") in PAY_METHODS else None
        svc.mark_sold(ctx.db, s, price=_dec(form.get("price")) if form.get("price") else None,
                      ttn=str(form.get("ttn", "")), pay_method=pm)
        flash(request, "Продано")
    ctx.db.commit()
    return _to(f"/razbor/parts/{s.stock_item_id}")


@router.get("/razbor/files/{name}")
def razbor_file(name: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.view", "parts.pick", "stock.view")
    path = (svc.PHOTO_DIR / name).resolve()
    if svc.PHOTO_DIR.resolve() not in path.parents or not path.exists():
        raise Forbidden()
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


# ======================= Экспорт =======================
@router.get("/razbor/export.{fmt}")
def export(fmt: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.export")
    rows = svc.export_rows(ctx.db, ctx.branch.id)
    stamp = date.today().strftime("%Y-%m-%d")
    makers = {"xlsx": (svc.export_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
              "csv": (svc.export_csv, "text/csv; charset=utf-8"), "json": (svc.export_json, "application/json"),
              "zip": (svc.export_zip, "application/zip")}
    if fmt not in makers:
        raise Forbidden()
    make, mime = makers[fmt]
    return Response(make(rows), media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="razbor-{stamp}.{fmt}"'})
