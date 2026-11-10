"""Шаг 7: разбор — доноры, б/у детали, продажа (резерв → продано), экспорт для сайта."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, select

from ..models import Donor, DonorPhoto, Location, PartCatalog, StockItem, StockMovement, StockPhoto, UsedSale, User
from ..models.money import PAY_METHODS
from ..models.razbor import CONDITIONS, DELIVERY, DONOR_STATUS, SALE_STATUS
from ..models.stock import MOVEMENT_TYPES, RAZBOR_CATEGORIES
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


def _catsub(form) -> tuple[str | None, str | None]:
    """Поле «catsub» = «Категория||Подкатегория» (один выпадающий список с группами). Нет поля — (None, None)."""
    if "catsub" not in form:
        return (str(form.get("category")) if form.get("category") is not None else None,
                str(form.get("subcategory")) if form.get("subcategory") is not None else None)
    c, _, s = str(form.get("catsub") or "").partition("||")
    return c.strip()[:60], s.strip()[:60]


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
    states = svc.item_states(db, ctx.branch.id, items, bal)
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([i.catalog_id for i in items])))}
    donors = {d.id: d for d in db.scalars(select(Donor).where(Donor.branch_id == ctx.branch.id))}
    locs = {l.id: l for l in _locations(ctx)}
    first = {}
    for p in db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id.in_([i.id for i in items])).order_by(StockPhoto.created_at)):
        first.setdefault(p.stock_item_id, p)
    rows = []
    for i in items:
        rows.append({"i": i, "c": cats.get(i.catalog_id), "d": donors.get(i.donor_id), "b": bal[i.id], "state": states[i.id],
                     "loc": ssvc.location_label(locs.get(i.location_id)), "photo": first.get(i.id)})
    return rows


RZ_FILTERS = {"": "Все", "stock": "В наличии", "reserved": "Резерв", "sent": "Отправлено", "sold": "Продано",
              "service": "В наряды СТО", "out": "Нет в наличии", "site": "На сайте"}


# ======================= Склад разбора: марка → модель → категория =======================
@router.get("/razbor")
def razbor(ctx: Ctx = Depends(get_ctx), q: str = "", f: str = "", donor: str = "", cat: str = "", sub: str = "",
           make: str = "", model: str = ""):
    ctx.need("razbor.view")
    db = ctx.db
    query = select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == ctx.branch.id, PartCatalog.kind == "used")
    if donor:
        query = query.where(StockItem.donor_id == _uuid(donor))
    all_rows = _item_rows(ctx, db.scalars(query.order_by(StockItem.code.desc())).all())
    rows = all_rows
    if q.strip():
        ql = q.strip().lower()
        rows = [r for r in rows if ql in f"{r['i'].code} {r['c'].name} {r['c'].article} {r['i'].donor_make} {r['i'].donor_model} {r['i'].donor_vin}".lower()]
    # Плитки — по всем деталям; дальше фильтр по состоянию
    from ..models.razbor import ITEM_STATES
    tiles = {k: sum(1 for r in all_rows if r["state"] == k) for k in ITEM_STATES}
    tiles["stock_sum"] = sum((r["i"].sale_price * max(r["b"]["on_hand"], 0) for r in all_rows if r["state"] in ("stock", "reserved")), Decimal(0))
    tiles["site"] = sum(1 for r in all_rows if r["i"].publish_on_site and r["state"] == "stock")
    if f == "site":
        rows = [r for r in rows if r["i"].publish_on_site and r["state"] == "stock"]
    elif f == "out":
        rows = [r for r in rows if r["state"] in ("sold", "service", "writeoff", "out")]
    elif f in ITEM_STATES:
        rows = [r for r in rows if r["state"] == f]
    # Дерево: марка → модель (по тому, что сейчас в выборке состояния)
    mk_tree = {}
    for r in rows:
        mk = r["i"].donor_make or "Без марки"
        md = r["i"].donor_model or "—"
        mk_tree.setdefault(mk, {"n": 0, "models": {}})
        mk_tree[mk]["n"] += 1
        mk_tree[mk]["models"][md] = mk_tree[mk]["models"].get(md, 0) + 1
    if make:
        rows = [r for r in rows if (r["i"].donor_make or "Без марки") == make]
    if model:
        rows = [r for r in rows if (r["i"].donor_model or "—") == model]
    cat_counts, sub_counts = {}, {}
    for r in rows:
        c = r["c"].category or "Прочее"
        cat_counts[c] = cat_counts.get(c, 0) + 1
        if c == cat and r["c"].subcategory:
            sub_counts[r["c"].subcategory] = sub_counts.get(r["c"].subcategory, 0) + 1
    if cat:
        rows = [r for r in rows if (r["c"].category or "Прочее") == cat]
    if sub:
        rows = [r for r in rows if (r["c"].subcategory or "") == sub]
    donors = db.scalars(select(Donor).where(Donor.branch_id == ctx.branch.id).order_by(Donor.created_at.desc())).all()
    eco = {d.id: svc.economy(db, ctx.branch.id, d) for d in donors}
    n_sales = {s: db.scalar(select(func.count()).select_from(UsedSale).where(UsedSale.branch_id == ctx.branch.id,
                                                                             UsedSale.status == s)) or 0
               for s in ("reserved", "sent")}
    ref = svc.tree(db, ctx.branch.id)
    keep = {"q": q, "f": f, "donor": donor, "make": make, "model": model, "cat": cat, "sub": sub}

    def link(**kw):
        from urllib.parse import urlencode
        d = {**keep, **kw}
        return "/razbor?" + urlencode({k: v for k, v in d.items() if v})
    return ctx.render("razbor/list.html", title="Разбор", rows=rows, q=q, f=f, donor=donor, donors=donors, eco=eco,
                      cat=cat, sub=sub, make=make, model=model, cat_counts=cat_counts, sub_counts=sub_counts,
                      mk_tree=dict(sorted(mk_tree.items())), ref=ref, tiles=tiles, link=link, FILTERS=RZ_FILTERS,
                      ITEM_STATES=ITEM_STATES, n_sales=n_sales, CONDITIONS=CONDITIONS, DONOR_STATUS=DONOR_STATUS,
                      RAZBOR_CATEGORIES=RAZBOR_CATEGORIES, locations=_locations(ctx), loc_label=ssvc.location_label,
                      n_site=tiles["site"] if ctx.can("razbor.export") else 0)


# ======================= Справочник: марки, модели, категории =======================
@router.get("/razbor/refs")
def refs_page(ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.manage")
    db = ctx.db
    used_make = {}
    used_cat = {}
    for mk, md, c, sc in db.execute(select(StockItem.donor_make, StockItem.donor_model, PartCatalog.category,
                                           PartCatalog.subcategory).join(PartCatalog, PartCatalog.id == StockItem.catalog_id)
                                    .where(StockItem.branch_id == ctx.branch.id, PartCatalog.kind == "used")):
        used_make[(mk or "").lower()] = used_make.get((mk or "").lower(), 0) + 1
        used_make[((mk or "") + "|" + (md or "")).lower()] = used_make.get(((mk or "") + "|" + (md or "")).lower(), 0) + 1
        used_cat[(c or "").lower()] = used_cat.get((c or "").lower(), 0) + 1
        used_cat[((c or "") + "|" + (sc or "")).lower()] = used_cat.get(((c or "") + "|" + (sc or "")).lower(), 0) + 1
    return ctx.render("razbor/refs.html", title="Разбор · справочник", ref=svc.tree(db, ctx.branch.id),
                      used_make=used_make, used_cat=used_cat)


@router.post("/razbor/refs")
def refs_save(ctx: Ctx = Depends(get_ctx), action: str = Form(...), rid: str = Form(""), name: str = Form(""),
              years: str = Form("")):
    """Добавить / переименовать / удалить марку, модель, категорию, подкатегорию.
    Переименование меняет подпись и у деталей; удалить можно только то, что не используется."""
    from ..models import RazborCategory, RazborMake, RazborModel
    ctx.need("razbor.manage")
    db, bid = ctx.db, ctx.branch.id
    name = name.strip()[:60]
    back = _to("/razbor/refs" + ("#cats" if "cat" in action or "sub" in action else "#makes"))

    def own(obj):
        if not obj or obj.branch_id != bid:
            raise Forbidden()
        return obj

    def used_parts(**where):
        q = select(func.count()).select_from(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
            StockItem.branch_id == bid, PartCatalog.kind == "used")
        for col, val in where.items():
            q = q.where(func.lower({"make": StockItem.donor_make, "model": StockItem.donor_model,
                                   "cat": PartCatalog.category, "sub": PartCatalog.subcategory}[col]) == val.lower())
        return db.scalar(q) or 0
    if action == "add_make" and name:
        svc.ensure_make_model(db, bid, name)
        flash(ctx.request, f"Марка «{name}» добавлена")
    elif action == "add_model" and name:
        mk = own(db.get(RazborMake, _uuid(rid)))
        _, md = svc.ensure_make_model(db, bid, mk.name, name)
        if md and years.strip():
            md.years = years.strip()[:40]
        flash(ctx.request, f"{mk.name} {name} — добавлено")
    elif action == "rename_make" and name:
        mk = own(db.get(RazborMake, _uuid(rid)))
        for i in db.scalars(select(StockItem).where(StockItem.branch_id == bid, func.lower(StockItem.donor_make) == mk.name.lower())):
            i.donor_make = name
        for d in db.scalars(select(Donor).where(Donor.branch_id == bid, func.lower(Donor.make) == mk.name.lower())):
            d.make = name
        mk.name = name
        flash(ctx.request, "Переименовано — у деталей и доноров тоже")
    elif action == "rename_model" and name:
        md = own(db.get(RazborModel, _uuid(rid)))
        mk = db.get(RazborMake, md.make_id)
        for i in db.scalars(select(StockItem).where(StockItem.branch_id == bid, func.lower(StockItem.donor_make) == mk.name.lower(),
                                                    func.lower(StockItem.donor_model) == md.name.lower())):
            i.donor_model = name
        for d in db.scalars(select(Donor).where(Donor.branch_id == bid, func.lower(Donor.make) == mk.name.lower(),
                                                func.lower(Donor.model) == md.name.lower())):
            d.model = name
        md.name = name
        md.years = years.strip()[:40]
        flash(ctx.request, "Сохранено")
    elif action == "del_make":
        mk = own(db.get(RazborMake, _uuid(rid)))
        n = used_parts(make=mk.name)
        if n:
            flash(ctx.request, f"У марки «{mk.name}» есть детали ({n}) — удалить нельзя. Можно переименовать.", "error")
            return back
        for md in db.scalars(select(RazborModel).where(RazborModel.make_id == mk.id)):
            db.delete(md)
        db.delete(mk)
        flash(ctx.request, f"Марка «{mk.name}» удалена")
    elif action == "del_model":
        md = own(db.get(RazborModel, _uuid(rid)))
        mk = db.get(RazborMake, md.make_id)
        n = used_parts(make=mk.name, model=md.name)
        if n:
            flash(ctx.request, f"У модели «{md.name}» есть детали ({n}) — удалить нельзя", "error")
            return back
        db.delete(md)
        flash(ctx.request, f"Модель «{md.name}» удалена")
    elif action in ("add_cat", "add_sub") and name:
        parent = own(db.get(RazborCategory, _uuid(rid))) if action == "add_sub" else None
        n = db.scalar(select(func.count()).select_from(RazborCategory).where(
            RazborCategory.branch_id == bid, RazborCategory.parent_id == (parent.id if parent else None)
            if parent else RazborCategory.parent_id.is_(None))) or 0
        db.add(RazborCategory(name=name, parent_id=parent.id if parent else None, sort=n + 1))
        flash(ctx.request, f"Добавлено: {name}")
    elif action == "rename_cat" and name:
        c = own(db.get(RazborCategory, _uuid(rid)))
        parent = db.get(RazborCategory, c.parent_id) if c.parent_id else None
        for p in db.scalars(select(PartCatalog).where(PartCatalog.kind == "used")):
            if parent and (p.category or "").lower() == parent.name.lower() and (p.subcategory or "").lower() == c.name.lower():
                p.subcategory = name
            elif not parent and (p.category or "").lower() == c.name.lower():
                p.category = name
        c.name = name
        flash(ctx.request, "Переименовано — у деталей тоже")
    elif action == "del_cat":
        c = own(db.get(RazborCategory, _uuid(rid)))
        parent = db.get(RazborCategory, c.parent_id) if c.parent_id else None
        n = used_parts(cat=parent.name, sub=c.name) if parent else used_parts(cat=c.name)
        if n:
            flash(ctx.request, f"В «{c.name}» есть детали ({n}) — сначала перенесите их в другую категорию", "error")
            return back
        db.delete(c)
        flash(ctx.request, f"«{c.name}» удалено")
    db.commit()
    return back


# ======================= Продажи и отправки =======================
SALE_TABS = {"reserved": "Резерв · собрать", "ship": "Отправить", "sent": "В пути · ждём деньги", "sold": "Продано",
             "returned": "Возвраты", "cancelled": "Отменено"}


@router.get("/razbor/sales")
def sales_page(ctx: Ctx = Depends(get_ctx), tab: str = "", q: str = ""):
    ctx.need("razbor.view")
    db = ctx.db
    from ..models.razbor import CHANNELS
    allsales = db.scalars(select(UsedSale).where(UsedSale.branch_id == ctx.branch.id)
                          .order_by(UsedSale.created_at.desc()).limit(500)).all()

    def tab_of(s):
        if s.status == "reserved":
            return "ship" if s.delivery in ("np", "other") else "reserved"
        return s.status
    counts = {k: sum(1 for s in allsales if tab_of(s) == k) for k in SALE_TABS}
    if tab not in SALE_TABS:
        tab = next((k for k in ("ship", "reserved", "sent") if counts[k]), "sold")
    rows = [s for s in allsales if tab_of(s) == tab]
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.id.in_([s.stock_item_id for s in rows])))}
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([i.catalog_id for i in items.values()])))}
    if q.strip():
        ql = q.strip().lower()
        rows = [s for s in rows if ql in f"{items[s.stock_item_id].code} {cats[items[s.stock_item_id].catalog_id].name} "
                                         f"{s.buyer_name} {s.buyer_phone} {s.ttn}".lower()]
    from datetime import datetime, timedelta, timezone
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    sums = {
        "sent": sum((s.price for s in allsales if s.status == "sent"), Decimal(0)),
        "sent_cod": sum((s.price for s in allsales if s.status == "sent" and s.cod and not s.paid), Decimal(0)),
        "sold30": sum((s.price for s in allsales if s.status == "sold" and s.sold_at and
                       (s.sold_at if s.sold_at.tzinfo else s.sold_at.replace(tzinfo=timezone.utc)) >= month_ago), Decimal(0)),
    }
    return ctx.render("razbor/sales.html", title="Разбор · продажи", tab=tab, TABS=SALE_TABS, counts=counts, rows=rows,
                      items=items, cats=cats, q=q, sums=sums, DELIVERY=DELIVERY, SALE_STATUS=SALE_STATUS,
                      PAY_METHODS=PAY_METHODS, CHANNELS=CHANNELS, fmt_phone=osvc.fmt_phone)


@router.get("/api/razbor/free")
def free_parts(ctx: Ctx = Depends(get_ctx), q: str = ""):
    """Поиск свободной б/у детали для новой продажи: код, название, марка, модель."""
    ctx.need("razbor.sell")
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"items": []}
    items = ctx.db.scalars(select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == ctx.branch.id, PartCatalog.kind == "used").order_by(StockItem.code.desc())).all()
    rows = [r for r in _item_rows(ctx, items) if r["state"] == "stock"
            and ql in f"{r['i'].code} {r['c'].name} {r['i'].donor_make} {r['i'].donor_model}".lower()][:12]
    return {"items": [{"id": str(r["i"].id), "code": r["i"].code, "name": r["c"].name,
                       "car": f"{r['i'].donor_make} {r['i'].donor_model} {r['i'].donor_year or ''}".strip(),
                       "price": float(r["i"].sale_price), "free": float(r["b"]["free"]), "loc": r["loc"]} for r in rows]}


@router.post("/razbor/sales/new")
async def sale_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Новая продажа с экрана «Продажи»: выбрали деталь → покупатель, доставка → в резерв или сразу продано."""
    ctx.need("razbor.sell")
    form = await request.form()
    i = _item(ctx, str(form.get("item_id", "")))
    return await _sell(request, ctx, i, form, back="/razbor/sales")


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
    svc.ensure_make_model(ctx.db, ctx.branch.id, d.make, d.model)   # новая марка/модель — сразу в справочник
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
    from ..models.razbor import ITEM_STATES
    return ctx.render("razbor/donor.html", title=f"{d.code} {d.make} {d.model}", d=d, rows=rows, photos=photos,
                      e=svc.economy(db, ctx.branch.id, d), locations=_locations(ctx), CONDITIONS=CONDITIONS,
                      DONOR_STATUS=DONOR_STATUS, loc_label=ssvc.location_label, ref=svc.tree(db, ctx.branch.id),
                      ITEM_STATES=ITEM_STATES)


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
                        publish=form.get("publish") == "1", article=str(form.get("article", "")),
                        category=_catsub(form)[0] or "", subcategory=_catsub(form)[1] or "")
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
    from ..models.razbor import CHANNELS, ITEM_STATES
    return ctx.render("razbor/part.html", title=f"{i.code} {row['c'].name}", r=row, i=i, photos=photos, sales=sales,
                      moves=moves, people=people, locations=_locations(ctx), CONDITIONS=CONDITIONS, DELIVERY=DELIVERY,
                      SALE_STATUS=SALE_STATUS, PAY_METHODS=PAY_METHODS, MOVEMENT_TYPES=MOVEMENT_TYPES,
                      loc_label=ssvc.location_label, fmt_phone=osvc.fmt_phone, ref=svc.tree(db, ctx.branch.id),
                      CHANNELS=CHANNELS, ITEM_STATES=ITEM_STATES)


@router.post("/razbor/parts/{iid}")
async def part_save(iid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.manage")
    i = _item(ctx, iid)
    form = await request.form(max_files=30)
    cat = ctx.db.get(PartCatalog, i.catalog_id)
    cat.name = str(form.get("name", cat.name)).strip()[:200] or cat.name
    c, s = _catsub(form)
    if c is not None:
        from ..models.stock import guess_razbor_category
        cat.category = c.strip()[:60] or guess_razbor_category(cat.name)
    if s is not None:
        cat.subcategory = s.strip()[:60]
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


async def _sell(request: Request, ctx: Ctx, i: StockItem, form, back: str):
    from ..models.razbor import CHANNELS
    action = str(form.get("action", "reserve"))
    args = dict(qty=_dec(form.get("qty"), "1"), price=_dec(form.get("price")), buyer_name=str(form.get("buyer_name", "")),
                buyer_phone=str(form.get("buyer_phone", "")),
                delivery=str(form.get("delivery", "pickup")) if form.get("delivery") in DELIVERY else "pickup",
                pay_method=str(form.get("pay_method", "cash")) if form.get("pay_method") in PAY_METHODS else "cash")
    if action == "sold":
        res = svc.sell_now(ctx.db, ctx.branch.id, i, ttn=str(form.get("ttn", "")), **args)
        if not isinstance(res, str):
            res.channel = str(form.get("channel", "")) if form.get("channel") in CHANNELS else ""
            res.paid = True
    else:
        res = svc.reserve_for_buyer(ctx.db, ctx.branch.id, i, comment=str(form.get("comment", "")),
                                    np_address=str(form.get("np_address", "")), cod=form.get("cod") == "1",
                                    channel=str(form.get("channel", "")) if form.get("channel") in CHANNELS else "", **args)
    if isinstance(res, str):
        flash(request, res, "error")
    else:
        ctx.db.commit()
        flash(request, f"{i.code} — продано" if action == "sold" else
              f"{i.code} в резерве под покупателя" + (" — теперь во вкладке «Отправить»" if args["delivery"] != "pickup" else ""))
    return _to(back)


@router.post("/razbor/parts/{iid}/sell")
async def part_sell(iid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Резерв под покупателя или сразу «Продано»."""
    ctx.need("razbor.sell")
    i = _item(ctx, iid)
    form = await request.form()
    return await _sell(request, ctx, i, form, back=f"/razbor/parts/{iid}")


@router.post("/razbor/parts/{iid}/writeoff")
def part_writeoff(iid: str, ctx: Ctx = Depends(get_ctx), qty: str = Form("1"), reason: str = Form("")):
    ctx.need("razbor.manage")
    i = _item(ctx, iid)
    err = svc.writeoff(ctx.db, ctx.branch.id, i, _dec(qty, "1"), reason)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Списано")
    return _to(f"/razbor/parts/{iid}")


@router.post("/razbor/sales/{sid}")
async def sale_update(sid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Резерв → продано (самовывоз) / отправлено (ТТН) → получено и оплачено / возврат; отмена резерва."""
    ctx.need("razbor.sell")
    s = ctx.db.get(UsedSale, _uuid(sid))
    if not s or s.branch_id != ctx.branch.id:
        raise Forbidden()
    form = await request.form()
    action = str(form.get("action", "sold"))
    back = str(form.get("back", ""))
    back = back if back.startswith("/") and not back.startswith("//") else f"/razbor/parts/{s.stock_item_id}"
    pm = str(form.get("pay_method")) if form.get("pay_method") in PAY_METHODS else None
    err, msg = "", ""
    if action == "cancel":
        svc.cancel_reserve(ctx.db, s)
        msg = "Резерв отменён — деталь снова свободна"
    elif action == "ship":
        err = svc.ship(ctx.db, s, str(form.get("ttn", "")), cod=form.get("cod") == "1" if "cod" in form else None,
                       paid=form.get("paid") == "1")
        msg = f"Отправлено, ТТН {s.ttn}" if s.ttn else "Отправлено"
    elif action == "complete":
        err = svc.complete(ctx.db, s, pm)
        msg = "Покупатель получил, деньги есть — продано"
    elif action == "return":
        err = svc.return_sale(ctx.db, s, str(form.get("note", "")))
        msg = "Возврат: деталь снова на складе разбора"
    elif action == "save":   # поправить данные покупателя / адрес / ТТН
        s.buyer_name = str(form.get("buyer_name", s.buyer_name)).strip()[:160]
        s.buyer_phone = osvc.norm_phone(str(form.get("buyer_phone", s.buyer_phone)))
        s.np_address = str(form.get("np_address", s.np_address or "")).strip()[:200]
        if form.get("ttn") is not None:
            s.ttn = str(form.get("ttn", "")).strip()[:40]
        if form.get("price") and s.status in ("reserved", "sent"):
            s.price = ssvc.money(_dec(form.get("price")))
        msg = "Сохранено"
    elif s.status == "reserved":
        svc.mark_sold(ctx.db, s, price=_dec(form.get("price")) if form.get("price") else None,
                      ttn=str(form.get("ttn", "")), pay_method=pm)
        s.paid = True
        msg = "Продано"
    if err:
        ctx.db.rollback()
        flash(request, err, "error")
    else:
        ctx.db.commit()
        if msg:
            flash(request, msg)
    return _to(back)


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
