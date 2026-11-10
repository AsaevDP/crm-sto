"""Шаг 5: подбор запчастей в наряд, закупки и приход, склад, запросы на выдачу."""
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..models import (IssueRequest, IssueRequestLine, Location, PartCatalog, Purchase, PurchaseLine, StockItem,
                      StockMovement, Supplier, User, Vehicle, WoPart, WorkOrder, WoWork)
from ..models.stock import CATEGORIES, MOVEMENT_TYPES, SECTIONS
from ..services import orders as osvc
from ..services import settings as st
from ..services import stock as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid

router = APIRouter()
PURCHASE_STATUS = {"draft": "Черновик", "ordered": "Заказана", "partial": "Пришла частично", "arrived": "Пришла"}


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return Decimal(default)


def _markup(v, ctx: Ctx) -> int:
    try:
        m = int(v)
    except (TypeError, ValueError):
        m = st.get(ctx.db, ctx.branch.id, "pricing")["default_markup"]
    return max(0, min(1000, m))  # наценку решает СТО: вписывается вручную, хоть 300 %


def _to(url):
    return RedirectResponse(url, status_code=303)


def _can_pick(ctx: Ctx, wo: WorkOrder) -> bool:
    return (ctx.can("parts.pick") and wo.status in ("picking", "waiting") and not wo.awaiting_prepay
            and (ctx.user.id == wo.partsman_id or ctx.can("orders.view_all")))


# ======================= Подбор в наряде =======================
@router.get("/api/catalog/search")
def catalog_search(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("parts.pick", "parts.offer", "stock.view", "purchases.manage")
    return {"items": svc.search_catalog(ctx.db, ctx.branch.id, q)}


# ======================= Бренды (11.11.2026) =======================
BRAND_EDIT = ("parts.pick", "parts.offer", "purchases.manage", "stock.manage", "settings.access")


@router.get("/api/brands")
def brands_list(ctx: Ctx = Depends(get_ctx)):
    ctx.need(*BRAND_EDIT, "stock.view")
    from ..services import brands
    return {"items": brands.names(ctx.db, ctx.branch.id)}


@router.post("/api/brands")
def brands_add(ctx: Ctx = Depends(get_ctx), name: str = Form("")):
    """Новый бренд прямо из окна запчасти — сразу виден всем."""
    ctx.need(*BRAND_EDIT)
    from ..services import brands
    b = brands.add(ctx.db, ctx.branch.id, name)
    if not b:
        return JSONResponse({"ok": False, "error": "Пустое название"}, status_code=400)
    ctx.db.commit()
    return {"ok": True, "name": b.name}


@router.get("/settings/brands")
def brands_page(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("settings.access", "stock.manage")
    from ..models import Brand
    from ..services import brands
    rows = ctx.db.scalars(select(Brand).where(Brand.branch_id == ctx.branch.id).order_by(func.lower(Brand.name))).all()
    used = dict(ctx.db.execute(select(PartCatalog.brand, func.count()).where(PartCatalog.brand != "")
                               .group_by(PartCatalog.brand)).all())
    return ctx.render("settings/brands.html", title="Бренды запчастей", rows=rows, used=used, q=q, norm=brands.norm)


@router.post("/settings/brands")
def brands_save(ctx: Ctx = Depends(get_ctx), action: str = Form("add"), name: str = Form(""), bid: str = Form("")):
    ctx.need("settings.access", "stock.manage")
    from ..models import Brand
    from ..services import brands
    if action == "add":
        if brands.find(ctx.db, ctx.branch.id, name):
            flash(ctx.request, f"«{brands.norm(name)}» уже есть в списке", "error")
        elif brands.add(ctx.db, ctx.branch.id, name):
            flash(ctx.request, f"Добавлен бренд «{brands.norm(name)}»")
    else:
        b = ctx.db.get(Brand, _uuid(bid))
        if not b or b.branch_id != ctx.branch.id:
            raise Forbidden()
        if action == "rename":
            err = brands.rename(ctx.db, b, name)
            flash(ctx.request, err or f"Переименовано: «{b.name}»", "error" if err else "ok")
        elif action == "delete":
            flash(ctx.request, f"Бренд «{b.name}» убран из списка (у запчастей он остаётся)")
            ctx.db.delete(b)
    ctx.db.commit()
    return _to("/settings/brands")


OFFER_STATUSES = ("created", "diag", "approve", "picking", "waiting", "instock", "work")


def _can_offer(ctx: Ctx, wo: WorkOrder) -> bool:
    """Менеджер (11.11.2026): сразу при согласовании вносит запчасти и варианты разных фирм — без закупки."""
    return ctx.can("parts.offer") and wo.status in OFFER_STATUSES


def _can_choose(ctx: Ctx, wo: WorkOrder) -> bool:
    return (ctx.can("orders.approve") or _can_pick(ctx, wo)) and wo.status in OFFER_STATUSES


@router.post("/orders/{order_id}/parts")
def part_add(order_id: str, ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(""), name: str = Form(""),
             article: str = Form(""), brand: str = Form(""), revision: str = Form(""), qty: str = Form("1"),
             cost: str = Form("0"), markup_pct: str = Form(""), price: str = Form(""), wo_work_id: str = Form(""),
             action: str = Form("auto"), order_qty: str = Form(""), option_group: str = Form("")):
    """Запчасть в наряд: из каталога или новая.
    Запчастист: со склада / заказать / вариант. Менеджер: «в наряд» (подберёт запчастист) или вариант на выбор клиенту."""
    ctx.need("parts.pick", "parts.offer")
    wo = _order(ctx, order_id)
    picker, offer = _can_pick(ctx, wo), _can_offer(ctx, wo)
    if not (picker or offer):
        raise Forbidden()
    if not picker and action in ("auto", "order", "stock"):
        action = "need"
    from ..services import brands
    brand = brands.norm(brand)
    if brand:
        brands.add(ctx.db, ctx.branch.id, brand)   # новый бренд — сразу в общий список
    cat = ctx.db.get(PartCatalog, _uuid(catalog_id)) if catalog_id else None
    if cat is None:
        if not name.strip():
            flash(ctx.request, "Название запчасти обязательно", "error")
            return _to(f"/orders/{order_id}")
        cat = svc.get_or_create_catalog(ctx.db, name, article, brand, revision)
    q = max(_dec(qty, "1"), Decimal("0.01"))
    if ctx.can("prices.view_purchase") and _dec(cost) > 0:
        c, m = svc.money(_dec(cost)), _markup(markup_pct, ctx)
        p = svc.sale_price(c, m)
    else:   # закупку не видит (менеджер) или не знает — пишет цену для клиента, запчастист потом уточнит
        c, m, p = svc.money(_dec(cost)), 0, svc.money(_dec(price))
    work = ctx.db.get(WoWork, _uuid(wo_work_id)) if wo_work_id else None
    part = WoPart(work_order_id=wo.id, catalog_id=cat.id, name=cat.name, qty=q, cost=c, markup_pct=m, price=p,
                  picked_by=ctx.user.id if picker else None,
                  wo_work_id=work.id if work and work.work_order_id == wo.id else None)
    ctx.db.add(part)
    ctx.db.flush()
    label = f"«{cat.name}»{' · ' + cat.brand if cat.brand else ''}"
    if action == "option":
        grp = option_group if option_group and ctx.db.scalar(select(WoPart.id).where(
            WoPart.work_order_id == wo.id, WoPart.option_group == option_group)) else uuid.uuid4().hex
        part.option_group, part.status = grp, "option"
        msg = f"{label} — вариант на выбор клиенту"
    elif action == "need":
        part.status = "need"
        msg = f"{label} — в наряде, запчастист подберёт" if not picker else f"{label} — добавлено, выберите: со склада или заказать"
    else:   # Есть свободный остаток — резервируем, иначе заказываем (если не просили иначе)
        if action in ("auto", "stock") and svc.reserve_from_stock(ctx.db, ctx.branch.id, part):
            msg = f"{label} — взято со склада в резерв"
        elif action in ("auto", "order"):
            svc.order_part(ctx.db, ctx.branch.id, part, _dec(order_qty) if order_qty else None)
            msg = f"{label} — добавлено в закупку"
        else:
            msg = f"{label} — добавлено, выберите: со склада или заказать"
    ctx.db.commit()
    flash(ctx.request, msg)
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/parts/{part_id}/choose")
def part_choose(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    """Клиент выбрал вариант: он идёт в подбор, остальные варианты группы — «не выбран»."""
    ctx.need("orders.approve", "parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not _can_choose(ctx, wo) or not part or part.work_order_id != wo.id or not part.option_group:
        raise Forbidden()
    err = svc.choose_option(ctx.db, ctx.branch.id, wo, part)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"Клиент выбрал: «{part.name}»")
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/parts/{part_id}/source")
def part_source(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx), action: str = Form("auto")):
    """Запчастист решает по позиции «нужна» (её внёс менеджер): со склада или заказать."""
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not _can_pick(ctx, wo) or not part or part.work_order_id != wo.id or part.status != "need":
        raise Forbidden()
    part.picked_by = part.picked_by or ctx.user.id
    msg = svc.source_part(ctx.db, ctx.branch.id, part, action if action in ("auto", "order") else "auto")
    ctx.db.commit()
    flash(ctx.request, f"«{part.name}» — {msg}")
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/parts/{part_id}/markup")
def part_markup(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx), markup_pct: str = Form(...),
                cost: str = Form("")):
    """Наценка по каждой позиции вручную (1–100 %). Цена продажи пересчитывается."""
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not part or part.work_order_id != wo.id or not ctx.can("prices.view_purchase"):
        return JSONResponse({"ok": False}, status_code=400)
    new_markup = _markup(markup_pct, ctx)
    # Цену клиенту назвал менеджер (закупки ещё нет): первая закупка не меняет цену — считаем наценку из неё
    keep_price = (cost.strip() and part.cost == 0 and part.price > 0 and new_markup == part.markup_pct)
    if cost.strip():
        part.cost = svc.money(_dec(cost))
    if keep_price and part.cost > 0:
        part.markup_pct = max(0, min(1000, int(round((part.price / part.cost - 1) * 100))))
    else:
        part.markup_pct = new_markup
        part.price = svc.sale_price(part.cost, part.markup_pct)
    ctx.db.commit()
    return {"ok": True, "price": str(part.price), "sum": str(svc.money(part.price * part.qty)), "markup": part.markup_pct}


@router.post("/orders/{order_id}/parts/{part_id}/delete")
def part_delete(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("parts.pick", "parts.offer")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    allowed = _can_pick(ctx, wo) or (_can_offer(ctx, wo) and part is not None and part.status in ("need", "option", "declined"))
    if not allowed or not part or part.work_order_id != wo.id or part.status in ("issued", "installed"):
        raise Forbidden()
    svc.release_part(ctx.db, part)
    ctx.db.delete(part)
    ctx.db.commit()
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/picking-done")
def picking_done(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    if wo.status == "picking" and _can_pick(ctx, wo):
        err = svc.finish_picking(ctx.db, wo)
        if err:
            ctx.db.rollback()
            flash(ctx.request, err, "error")
        else:
            ctx.db.commit()
            flash(ctx.request, "Подбор завершён")
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/issue-request")
def issue_request(order_id: str, ctx: Ctx = Depends(get_ctx), comment: str = Form("")):
    """Слесарь или менеджер просит выдать зарезервированные запчасти."""
    ctx.need("stock.request_issue")
    wo = _order(ctx, order_id)
    req = svc.request_issue(ctx.db, wo, comment)
    if req:
        ctx.db.commit()
        flash(ctx.request, "Запрос отправлен кладовщику")
    else:
        flash(ctx.request, "Нечего выдавать: нет запчастей в резерве", "error")
    return _to(f"/orders/{order_id}")


# ======================= Закупки =======================
@router.get("/purchases")
def purchases(ctx: Ctx = Depends(get_ctx)):
    ctx.need("purchases.view")
    rows = ctx.db.scalars(select(Purchase).where(Purchase.branch_id == ctx.branch.id)
                          .order_by(Purchase.status != "draft", Purchase.created_at.desc()).limit(100)).all()
    ids = [p.id for p in rows]
    stats = {pid: (n, total) for pid, n, total in ctx.db.execute(
        select(PurchaseLine.purchase_id, func.count(), func.sum(PurchaseLine.qty * PurchaseLine.price))
        .where(PurchaseLine.purchase_id.in_(ids)).group_by(PurchaseLine.purchase_id))} if ids else {}
    suppliers = {s.id: s for s in ctx.db.scalars(select(Supplier).where(Supplier.branch_id == ctx.branch.id))}
    return ctx.render("stock/purchases.html", title="Закупки", rows=rows, stats=stats, suppliers=suppliers,
                      PURCHASE_STATUS=PURCHASE_STATUS)


@router.post("/purchases/new")
def purchase_new(ctx: Ctx = Depends(get_ctx)):
    ctx.need("purchases.manage")
    p = svc.draft_purchase(ctx.db, ctx.branch.id)
    ctx.db.commit()
    return _to(f"/purchases/{p.id}")


def _purchase(ctx: Ctx, pid: str) -> Purchase:
    p = ctx.db.get(Purchase, _uuid(pid))
    if not p or p.branch_id != ctx.branch.id:
        raise Forbidden()
    return p


@router.get("/purchases/{pid}")
def purchase_view(pid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("purchases.view")
    p = _purchase(ctx, pid)
    db = ctx.db
    lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id).order_by(PurchaseLine.created_at)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([l.catalog_id for l in lines])))}
    parts = {x.id: x for x in db.scalars(select(WoPart).where(WoPart.id.in_([l.wo_part_id for l in lines if l.wo_part_id])))}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([x.work_order_id for x in parts.values()])))}
    locations = db.scalars(select(Location).where(Location.branch_id == ctx.branch.id).order_by(Location.rack, Location.shelf, Location.place)).all()
    from ..services import brands as _br
    return ctx.render("stock/purchase.html", title="Закупка", p=p, lines=lines, cats=cats, parts=parts, orders=orders,
                      BRANDS=_br.names(db, ctx.branch.id),
                      suppliers=db.scalars(select(Supplier).where(Supplier.branch_id == ctx.branch.id).order_by(Supplier.name)).all(),
                      supplier=db.get(Supplier, p.supplier_id) if p.supplier_id else None,
                      locations=locations, loc_label=svc.location_label, PURCHASE_STATUS=PURCHASE_STATUS,
                      total=sum((l.qty * l.price for l in lines), Decimal(0)))


@router.post("/purchases/{pid}")
def purchase_save(pid: str, ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form(""),
                  supplier_invoice: str = Form(""), note: str = Form(""), action: str = Form("save")):
    ctx.need("purchases.manage")
    p = _purchase(ctx, pid)
    if supplier_new.strip():
        s = Supplier(name=supplier_new.strip()[:160])
        ctx.db.add(s)
        ctx.db.flush()
        p.supplier_id = s.id
    elif supplier_id:
        s = ctx.db.get(Supplier, uuid.UUID(supplier_id))
        p.supplier_id = s.id if s and s.branch_id == ctx.branch.id else None
    p.supplier_invoice, p.note = supplier_invoice.strip()[:80], note.strip()
    if action == "ordered" and p.status == "draft":
        has_lines = ctx.db.scalar(select(func.count()).select_from(PurchaseLine).where(PurchaseLine.purchase_id == p.id))
        if not has_lines:
            flash(ctx.request, "В закупке нет строк", "error")
            return _to(f"/purchases/{pid}")
        p.status = "ordered"
        flash(ctx.request, "Закупка отправлена поставщику — кладовщик ждёт приход")
    ctx.db.commit()
    return _to(f"/purchases/{pid}")


@router.post("/purchases/{pid}/lines")
def purchase_line_add(pid: str, ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(""), name: str = Form(""),
                      article: str = Form(""), brand: str = Form(""), revision: str = Form(""),
                      qty: str = Form("1"), price: str = Form("0")):
    """Строка «на склад» (опт впрок, без наряда)."""
    ctx.need("purchases.manage")
    p = _purchase(ctx, pid)
    if p.status != "draft":
        raise Forbidden()
    cat = ctx.db.get(PartCatalog, uuid.UUID(catalog_id)) if catalog_id else None
    if cat is None:
        if not name.strip():
            flash(ctx.request, "Название обязательно", "error")
            return _to(f"/purchases/{pid}")
        cat = svc.get_or_create_catalog(ctx.db, name, article, brand, revision)
    ctx.db.add(PurchaseLine(purchase_id=p.id, catalog_id=cat.id, qty=max(_dec(qty, "1"), Decimal("0.01")),
                            price=svc.money(_dec(price))))
    ctx.db.commit()
    return _to(f"/purchases/{pid}")


@router.post("/purchases/{pid}/lines/{lid}")
def purchase_line_edit(pid: str, lid: str, ctx: Ctx = Depends(get_ctx), qty: str = Form(...), price: str = Form(...),
                       delete: str = Form("")):
    ctx.need("purchases.manage")
    p = _purchase(ctx, pid)
    line = ctx.db.get(PurchaseLine, _uuid(lid))
    if p.status != "draft" or not line or line.purchase_id != p.id:
        raise Forbidden()
    part = ctx.db.get(WoPart, line.wo_part_id) if line.wo_part_id else None
    if delete:
        if part:
            part.status = "need"   # позиция наряда снова ждёт решения запчастиста
        ctx.db.delete(line)
    else:
        line.qty = max(_dec(qty, "1"), part.qty if part else Decimal("0.01"))  # под наряд — не меньше нужного
        line.price = svc.money(_dec(price))
        if part and part.cost != line.price:
            part.cost = line.price
            part.price = svc.sale_price(part.cost, part.markup_pct)
    ctx.db.commit()
    return _to(f"/purchases/{pid}")


@router.post("/purchases/{pid}/receive")
def purchase_receive(pid: str, request_ctx: Ctx = Depends(get_ctx), location_id: str = Form(""),
                     line_id: str = Form("")):
    """Приход: кладовщик принимает строку (или все) в ячейку. Под наряд — сразу резерв."""
    ctx = request_ctx
    ctx.need("stock.manage")
    p = _purchase(ctx, pid)
    if p.status not in ("ordered", "partial"):
        raise Forbidden()
    loc = ctx.db.get(Location, uuid.UUID(location_id)) if location_id else None
    loc_id = loc.id if loc and loc.branch_id == ctx.branch.id else None
    q = select(PurchaseLine).where(PurchaseLine.purchase_id == p.id, PurchaseLine.received.is_(False))
    if line_id:
        q = q.where(PurchaseLine.id == _uuid(line_id))
    n = 0
    for line in ctx.db.scalars(q).all():
        svc.receive_line(ctx.db, ctx.branch.id, line, loc_id)
        n += 1
    ctx.db.flush()
    svc.refresh_purchase_status(ctx.db, p)
    ctx.db.commit()
    flash(ctx.request, f"Принято строк: {n}")
    return _to(f"/purchases/{pid}")


# ======================= Склад =======================
def _stock_rows(ctx: Ctx, q: str, only: str, sec: str, cat: str, sub: str, make: str, model: str):
    """Строки склада с фильтрами. Б/у (разбор) здесь не показываются — они в разделе «Разбор»."""
    from ..services import stock_cat as sc
    db = ctx.db
    items = db.scalars(select(StockItem).where(StockItem.branch_id == ctx.branch.id)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.branch_id == ctx.branch.id,
                                                                  PartCatalog.kind != "used"))}
    locs = {l.id: l for l in db.scalars(select(Location).where(Location.branch_id == ctx.branch.id))}
    fits = sc.fits_map(db, cats.keys())
    bal = svc.balances(db, ctx.branch.id)
    rows, counts = [], {}
    ql = q.strip().lower()
    for i in items:
        c = cats.get(i.catalog_id)
        if not c or (c.section or "repair") != sec:
            continue
        b = bal[i.id]
        if only == "free" and b["free"] <= 0:
            continue
        if only != "all" and b["on_hand"] == 0 and b["reserved"] == 0:
            continue
        fl = fits.get(c.id, [])
        if ql and ql not in f"{c.name} {c.article} {c.brand} {c.fits} {i.barcode or ''} {' '.join(sc.fit_label(f) for f in fl)}".lower():
            continue
        if make and not any(f.make == make and (not model or f.model == model) for f in fl):
            continue
        key = (c.category or "", c.subcategory or "")
        counts[key[0]] = counts.get(key[0], 0) + 1
        counts[key] = counts.get(key, 0) + 1
        if cat and key[0] != ("" if cat == "-" else cat):   # «-» — без категории
            continue
        if sub and key[1] != sub:
            continue
        rows.append({"i": i, "c": c, "loc": svc.location_label(locs.get(i.location_id)), "fits": fl, **b})
    rows.sort(key=lambda r: (r["c"].category or "яя", r["c"].subcategory or "", r["c"].name.lower(), r["loc"]))
    return rows, counts


def _stock_args(sec, cat, sub, make, model, q, only):
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in dict(sec=sec, cat=cat, sub=sub, make=make, model=model, q=q, only=only).items() if v})


@router.get("/stock")
def stock_list(ctx: Ctx = Depends(get_ctx), q: str = "", only: str = "", sec: str = "to", cat: str = "", sub: str = "",
               make: str = "", model: str = ""):
    """Склад (10.11.2026): вкладки «ТО» и «Запчасти», дерево категорий, фильтр по марке и модели, штрихкоды."""
    ctx.need("stock.view")
    from ..services import stock_cat as sc
    db = ctx.db
    sec = sec if sec in SECTIONS else "to"
    hit = sc.by_barcode(db, ctx.branch.id, q)   # сканер «напечатал» штрихкод — сразу в карточку
    if hit:
        return _to(f"/stock/items/{hit.id}")
    sc.seed(db, ctx.branch.id)
    db.commit()
    rows, counts = _stock_rows(ctx, q, only, sec, cat, sub, make, model)
    new_requests = db.scalar(select(func.count()).select_from(IssueRequest)
                             .where(IssueRequest.branch_id == ctx.branch.id, IssueRequest.status == "new"))
    n_locations = db.scalar(select(func.count()).select_from(Location).where(Location.branch_id == ctx.branch.id))
    mm = sc.makes_models(db, ctx.branch.id)
    return ctx.render("stock/list.html", title="Склад", rows=rows, q=q, only=only, new_requests=new_requests,
                      sec=sec, cat=cat, sub=sub, make=make, model=model, tree=sc.tree(db, ctx.branch.id, sec),
                      counts=counts, makes=mm, models=mm.get(make, []), n_locations=n_locations, fit_label=sc.fit_label,
                      args=lambda **o: _stock_args(**{**dict(sec=sec, cat=cat, sub=sub, make=make, model=model, q=q, only=only), **o}))


@router.get("/stock/print")
def stock_print(ctx: Ctx = Depends(get_ctx), q: str = "", only: str = "", sec: str = "to", cat: str = "", sub: str = "",
                make: str = "", model: str = ""):
    """Список склада на A4 с текущими фильтрами (для инвентаризации — графа «Факт»)."""
    ctx.need("stock.view")
    from ..services import stock_cat as sc
    sec = sec if sec in SECTIONS else "to"
    rows, _ = _stock_rows(ctx, q, only, sec, cat, sub, make, model)
    filt = " · ".join(x for x in (SECTIONS[sec], cat, sub, " ".join(x for x in (make, model) if x), f"поиск «{q}»" if q else "") if x)
    return ctx.render("stock/print.html", title=f"Склад — {SECTIONS[sec]}", rows=rows, filt=filt, fit_label=sc.fit_label,
                      show_cost=ctx.can("prices.view_purchase"), back="/stock?" + _stock_args(sec, cat, sub, make, model, q, only))


@router.get("/stock/export.xlsx")
def stock_xlsx(ctx: Ctx = Depends(get_ctx), q: str = "", only: str = "", sec: str = "to", cat: str = "", sub: str = "",
               make: str = "", model: str = ""):
    """Склад в Excel (.xlsx) — с теми же фильтрами, что на экране."""
    ctx.need("stock.view")
    import io
    from fastapi.responses import Response
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from ..services import stock_cat as sc
    sec = sec if sec in SECTIONS else "to"
    rows, _ = _stock_rows(ctx, q, only, sec, cat, sub, make, model)
    show_cost = ctx.can("prices.view_purchase")
    wb = Workbook()
    ws = wb.active
    ws.title = SECTIONS[sec][:31]
    head = ["Штрихкод", "Запчасть", "Категория", "Подкатегория", "Бренд", "Артикул", "Подходит для", "Ячейка",
            "На складе", "Резерв", "Свободно", *(["Закупка, ₴"] if show_cost else [])]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEEEF2")
    for r in rows:
        ws.append([r["i"].barcode or "", r["c"].name, r["c"].category, r["c"].subcategory or "", r["c"].brand, r["c"].article,
                   "; ".join(sc.fit_label(f) for f in r["fits"]) or r["c"].fits, r["loc"], float(r["on_hand"]),
                   float(r["reserved"]), float(r["free"]), *([float(r["i"].cost or 0)] if show_cost else [])])
    for col, w in zip("ABCDEFGHIJKL", (15, 36, 18, 18, 12, 16, 30, 12, 10, 9, 10, 12)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="sklad_{sec}.xlsx"'})


@router.get("/stock/items/{item_id}")
def stock_item(item_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.view")
    from ..services import stock_cat as sc
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    c = ctx.db.get(PartCatalog, i.catalog_id)
    moves = ctx.db.scalars(select(StockMovement).where(StockMovement.stock_item_id == i.id)
                           .order_by(StockMovement.created_at.desc()).limit(200)).all()
    orders = {o.id: o for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.id.in_([m.work_order_id for m in moves if m.work_order_id])))}
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_([m.created_by for m in moves if m.created_by])))}
    sc.seed(ctx.db, ctx.branch.id)
    ctx.db.commit()
    from ..services import barcode as bc
    return ctx.render("stock/item.html", title=c.name, i=i, c=c, b=svc.balances(ctx.db, ctx.branch.id, [i.id])[i.id],
                      loc=svc.location_label(ctx.db.get(Location, i.location_id) if i.location_id else None),
                      moves=moves, orders=orders, people=people, MOVEMENT_TYPES=MOVEMENT_TYPES,
                      tree_json={s: [{"name": x["c"].name, "subs": [y.name for y in x["subs"]]}
                                     for x in sc.tree(ctx.db, ctx.branch.id, s)] for s in SECTIONS},
                      fits=sc.fits_map(ctx.db, [c.id]).get(c.id, []), fit_label=sc.fit_label,
                      barcode_svg=bc.svg(i.barcode) if i.barcode else "", makes=osvc.known_makes(ctx.db, ctx.branch.id))


@router.post("/stock/items/{item_id}/category")
def stock_category(item_id: str, ctx: Ctx = Depends(get_ctx), section: str = Form(""), category: str = Form(""),
                   subcategory: str = Form("")):
    """Раздел (ТО / Запчасти), категория и подкатегория позиции склада."""
    ctx.need("stock.manage", "parts.pick")
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    c = ctx.db.get(PartCatalog, i.catalog_id)
    if c.kind != "used" and section in SECTIONS:
        c.section = section
    c.category = category.strip()[:60]
    c.subcategory = subcategory.strip()[:60]
    ctx.db.commit()
    flash(ctx.request, "Категория сохранена")
    return _to(f"/stock/items/{item_id}")


@router.post("/stock/items/{item_id}/fits")
def stock_fit_add(item_id: str, ctx: Ctx = Depends(get_ctx), make: str = Form(...), model: str = Form(""),
                  year_from: str = Form(""), year_to: str = Form("")):
    """«Подходит для»: марка, модель, годы."""
    ctx.need("stock.manage", "parts.pick")
    from ..models import PartFit
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id or not make.strip():
        raise Forbidden()
    yr = lambda v: int(v) if v.strip().isdigit() and 1950 <= int(v) <= 2100 else None  # noqa: E731
    ctx.db.add(PartFit(catalog_id=i.catalog_id, make=make.strip()[:60], model=model.strip()[:60],
                       year_from=yr(year_from), year_to=yr(year_to)))
    ctx.db.commit()
    return _to(f"/stock/items/{item_id}")


@router.post("/stock/items/{item_id}/fits/{fit_id}/delete")
def stock_fit_delete(item_id: str, fit_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.manage", "parts.pick")
    from ..models import PartFit
    f = ctx.db.get(PartFit, _uuid(fit_id))
    i = ctx.db.get(StockItem, _uuid(item_id))
    if f and i and f.catalog_id == i.catalog_id and i.branch_id == ctx.branch.id:
        ctx.db.delete(f)
        ctx.db.commit()
    return _to(f"/stock/items/{item_id}")


# ---------- Дерево категорий ----------
@router.get("/stock/categories")
def stock_categories(ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.view")
    from ..services import stock_cat as sc
    sc.seed(ctx.db, ctx.branch.id)
    ctx.db.commit()
    trees = {s: sc.tree(ctx.db, ctx.branch.id, s) for s in SECTIONS}
    used = {}
    for s in trees.values():
        for x in s:
            used[x["c"].id] = sc.usage(ctx.db, x["c"])
            for y in x["subs"]:
                used[y.id] = sc.usage(ctx.db, y)
    return ctx.render("stock/categories.html", title="Категории склада", trees=trees, used=used,
                      can_edit=ctx.can("stock.manage"))


@router.post("/stock/categories")
def stock_category_add(ctx: Ctx = Depends(get_ctx), section: str = Form(...), name: str = Form(...), parent_id: str = Form("")):
    ctx.need("stock.manage")
    from ..models import StockCategory
    parent = ctx.db.get(StockCategory, _uuid(parent_id)) if parent_id else None
    if section in SECTIONS and name.strip() and (not parent or parent.branch_id == ctx.branch.id):
        ctx.db.add(StockCategory(section=parent.section if parent else section, name=name.strip()[:60],
                                 parent_id=parent.id if parent else None, sort=99))
        ctx.db.commit()
        flash(ctx.request, f"Добавлено: {name.strip()}")
    return _to("/stock/categories")


@router.post("/stock/categories/{cid}")
def stock_category_edit(cid: str, ctx: Ctx = Depends(get_ctx), action: str = Form("rename"), name: str = Form("")):
    """Переименовать (вместе с запчастями этой категории) или удалить (только пустую)."""
    ctx.need("stock.manage")
    from ..models import StockCategory
    from ..services import stock_cat as sc
    c = ctx.db.get(StockCategory, _uuid(cid))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    if action == "delete":
        subs = ctx.db.scalars(select(StockCategory).where(StockCategory.parent_id == c.id)).all()
        n = sc.usage(ctx.db, c) + sum(sc.usage(ctx.db, s) for s in subs)
        if n:
            flash(ctx.request, f"Нельзя удалить: в «{c.name}» есть запчасти ({n}). Сначала перенесите их в другую категорию.", "error")
        else:
            for s in subs:
                ctx.db.delete(s)
            ctx.db.delete(c)
            ctx.db.commit()
            flash(ctx.request, f"Удалено: {c.name}")
    else:
        sc.rename(ctx.db, c, name)
        ctx.db.commit()
        flash(ctx.request, "Переименовано")
    return _to("/stock/categories")


# ---------- Наклейки со штрихкодом ----------
@router.get("/stock/labels")
def stock_labels(ctx: Ctx = Depends(get_ctx), ids: str = "", copies: int = 1):
    """Наклейки: размер и режим (принтер наклеек / лист A4) — в «Настройки → Наклейки». Принтер выбирается
    в окне печати браузера — когда купите, выберите его там."""
    ctx.need("stock.view")
    from ..services import barcode as bc
    from ..services import stock_cat as sc
    want = [x for x in (_uuid(v) for v in ids.split(",") if v.strip()) if x]
    items = [i for i in ctx.db.scalars(select(StockItem).where(StockItem.id.in_(want))) if i.branch_id == ctx.branch.id]
    labels = []
    for i in items:
        if not i.barcode:
            i.barcode = sc.barcode_for(i.id)
        c = ctx.db.get(PartCatalog, i.catalog_id)
        loc = svc.location_label(ctx.db.get(Location, i.location_id) if i.location_id else None)
        fits = sc.fits_map(ctx.db, [c.id]).get(c.id, [])
        lab = {"i": i, "c": c, "loc": loc, "svg": bc.svg(i.barcode), "fits": ", ".join(sc.fit_label(f) for f in fits[:2]) or c.fits}
        labels += [lab] * max(1, min(copies, 50))
    ctx.db.commit()
    return ctx.render("stock/labels.html", title="Наклейки", labels=labels, cfg=st.get(ctx.db, ctx.branch.id, "labels"),
                      back=ctx.request.headers.get("referer", "/stock"))


@router.get("/settings/labels")
def labels_settings(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access", "stock.manage")
    return ctx.render("settings/labels.html", title="Наклейки", cfg=st.get(ctx.db, ctx.branch.id, "labels"))


@router.post("/settings/labels")
def labels_save(ctx: Ctx = Depends(get_ctx), size: str = Form("100x70"), width_mm: str = Form(""), height_mm: str = Form(""),
                mode: str = Form("label"), printer: str = Form(""), show_price: str = Form(""), show_cell: str = Form(""),
                show_fits: str = Form("")):
    ctx.need("settings.access", "stock.manage")
    try:
        w, h = (int(x) for x in size.split("x")) if size != "custom" else (int(width_mm), int(height_mm))
    except ValueError:
        w, h = 100, 70
    st.put(ctx.db, ctx.branch.id, "labels", {"width_mm": max(20, min(w, 210)), "height_mm": max(15, min(h, 297)),
                                             "mode": mode if mode in ("label", "sheet") else "label",
                                             "printer": printer.strip()[:80], "show_price": bool(show_price),
                                             "show_cell": bool(show_cell), "show_fits": bool(show_fits)})
    ctx.db.commit()
    flash(ctx.request, "Настройки наклеек сохранены")
    return _to("/settings/labels")


@router.post("/stock/items/{item_id}/adjust")
def stock_adjust(item_id: str, ctx: Ctx = Depends(get_ctx), actual: str = Form(...), reason: str = Form("")):
    """Инвентаризация: «по факту на полке N» → корректировка разницы. Причина обязательна."""
    ctx.need("stock.manage")
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    if not reason.strip():
        flash(ctx.request, "Укажите причину корректировки", "error")
        return _to(f"/stock/items/{item_id}")
    b = svc.balances(ctx.db, ctx.branch.id, [i.id])[i.id]
    diff = _dec(actual) - b["on_hand"]
    if diff:
        svc.move(ctx.db, "adjust", i, diff, comment=f"Инвентаризация: {reason.strip()}")
        ctx.db.commit()
        flash(ctx.request, f"Остаток исправлен на {diff:+}")
    return _to(f"/stock/items/{item_id}")


@router.get("/stock/locations")
def locations(ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.view")
    rows = ctx.db.scalars(select(Location).where(Location.branch_id == ctx.branch.id)
                          .order_by(Location.rack, Location.shelf, Location.place)).all()
    return ctx.render("stock/locations.html", title="Ячейки склада", rows=rows, loc_label=svc.location_label)


@router.post("/stock/locations")
def location_add(ctx: Ctx = Depends(get_ctx), rack: str = Form(...), shelf: str = Form(""), place: str = Form(""),
                 shelves: str = Form(""), places: str = Form("")):
    """Ячейка или сразу весь стеллаж: «B», полок 4, мест 5 → 20 ячеек."""
    ctx.need("stock.manage")
    rack = rack.strip().upper()[:10]
    if not rack:
        return _to("/stock/locations")
    existing = {(l.rack, l.shelf, l.place) for l in ctx.db.scalars(select(Location).where(Location.branch_id == ctx.branch.id))}
    combos = [(shelf.strip()[:10], place.strip()[:10])]
    if shelves.strip().isdigit() and places.strip().isdigit():
        combos = [(str(s), str(p)) for s in range(1, min(int(shelves), 30) + 1) for p in range(1, min(int(places), 50) + 1)]
    n = 0
    for s, p in combos:
        if (rack, s, p) not in existing:
            ctx.db.add(Location(rack=rack, shelf=s, place=p, label=" / ".join(x for x in (rack, s, p) if x)))
            n += 1
    ctx.db.commit()
    flash(ctx.request, f"Добавлено ячеек: {n}")
    return _to("/stock/locations")


# ======================= Выдача =======================
def _requests(ctx: Ctx, status="new"):
    db = ctx.db
    reqs = db.scalars(select(IssueRequest).where(IssueRequest.branch_id == ctx.branch.id, IssueRequest.status == status)
                      .order_by(IssueRequest.created_at.desc() if status != "new" else IssueRequest.created_at).limit(50)).all()
    out = []
    for r in reqs:
        wo = db.get(WorkOrder, r.work_order_id)
        lines = []
        for l in db.scalars(select(IssueRequestLine).where(IssueRequestLine.request_id == r.id)):
            part = db.get(WoPart, l.wo_part_id)
            cat = db.get(PartCatalog, part.catalog_id) if part and part.catalog_id else None
            cells = []
            for sid, qty in svc.reserved_for_part(db, part.id).items():
                si = db.get(StockItem, sid)
                cells.append(f"{svc.location_label(db.get(Location, si.location_id) if si.location_id else None)} × {qty.normalize()}")
            lines.append({"name": part.name if part else "?", "article": cat.article if cat else "", "qty": l.qty,
                          "cells": cells, "status": part.status if part else ""})
        out.append({"r": r, "wo": wo, "v": db.get(Vehicle, wo.vehicle_id), "who": db.get(User, r.created_by),
                    "lines": lines})
    return out


@router.get("/stock/issues")
def issues(ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.issue", "stock.view")
    return ctx.render("stock/issues.html", title="Выдача", new=_requests(ctx, "new"), done=_requests(ctx, "issued")[:15],
                      fmt_plate=osvc.fmt_plate)


@router.post("/stock/issues/{rid}/issue")
def issue_do(rid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.issue")
    r = ctx.db.get(IssueRequest, _uuid(rid))
    if r and r.branch_id == ctx.branch.id and r.status == "new":
        svc.issue(ctx.db, r, ctx.user.id)
        ctx.db.commit()
        flash(ctx.request, "Выдано")
    return _to("/stock/issues")


@router.get("/api/notify")
def notify(ctx: Ctx = Depends(get_ctx)):
    """Окошко у кладовщика: новые запросы на выдачу (опрос раз в 15 секунд)."""
    if not ctx.can("stock.issue"):
        return {"issues": 0, "items": []}
    items = []
    for x in _requests(ctx, "new")[:5]:
        names = ", ".join(l["name"] for l in x["lines"][:3]) + ("…" if len(x["lines"]) > 3 else "")
        items.append({"id": str(x["r"].id), "who": x["who"].full_name if x["who"] else "",
                      "text": names, "order": x["wo"].number, "plate": osvc.fmt_plate(x["v"].plate)})
    return {"issues": len(_requests(ctx, "new")), "items": items}


# ======================= Настройки цен =======================
@router.get("/settings/pricing")
def pricing(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    return ctx.render("settings/pricing.html", title="Цены и проценты", pricing=st.get(ctx.db, ctx.branch.id, "pricing"),
                      payroll=st.get(ctx.db, ctx.branch.id, "payroll"), is_admin=ctx.user.role.code == "admin")


@router.post("/settings/pricing")
def pricing_save(ctx: Ctx = Depends(get_ctx), default_markup: str = Form("25"), hour_rate: str = Form("")):
    ctx.need("settings.access")
    try:
        value = {"default_markup": max(0, min(1000, int(default_markup)))}
        if hour_rate.strip():
            value["hour_rate"] = max(0, int(_dec(hour_rate)))
        st.put(ctx.db, ctx.branch.id, "pricing", value)
    except ValueError:
        pass
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _to("/settings/pricing")
