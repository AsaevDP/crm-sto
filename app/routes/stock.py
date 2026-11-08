"""Шаг 5: подбор запчастей в наряд, закупки и приход, склад, запросы на выдачу."""
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..models import (IssueRequest, IssueRequestLine, Location, PartCatalog, Purchase, PurchaseLine, StockItem,
                      StockMovement, Supplier, User, Vehicle, WoPart, WorkOrder, WoWork)
from ..models.stock import MOVEMENT_TYPES
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
    return max(1, min(100, m))


def _to(url):
    return RedirectResponse(url, status_code=303)


def _can_pick(ctx: Ctx, wo: WorkOrder) -> bool:
    return (ctx.can("parts.pick") and wo.status in ("picking", "waiting")
            and (ctx.user.id == wo.partsman_id or ctx.can("orders.view_all")))


# ======================= Подбор в наряде =======================
@router.get("/api/catalog/search")
def catalog_search(ctx: Ctx = Depends(get_ctx), q: str = ""):
    ctx.need("parts.pick", "stock.view", "purchases.manage")
    return {"items": svc.search_catalog(ctx.db, ctx.branch.id, q)}


@router.post("/orders/{order_id}/parts")
def part_add(order_id: str, ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(""), name: str = Form(""),
             article: str = Form(""), brand: str = Form(""), revision: str = Form(""), qty: str = Form("1"),
             cost: str = Form("0"), markup_pct: str = Form(""), wo_work_id: str = Form(""),
             action: str = Form("auto"), order_qty: str = Form("")):
    """Запчастист добавляет позицию: из каталога или новую (название, артикул, цена закупки, наценка)."""
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    if not _can_pick(ctx, wo):
        raise Forbidden()
    cat = ctx.db.get(PartCatalog, uuid.UUID(catalog_id)) if catalog_id else None
    if cat is None:
        if not name.strip():
            flash(ctx.request, "Название запчасти обязательно", "error")
            return _to(f"/orders/{order_id}")
        cat = svc.get_or_create_catalog(ctx.db, name, article, brand, revision)
    q, c, m = max(_dec(qty, "1"), Decimal("0.01")), svc.money(_dec(cost)), _markup(markup_pct, ctx)
    work = ctx.db.get(WoWork, uuid.UUID(wo_work_id)) if wo_work_id else None
    part = WoPart(work_order_id=wo.id, catalog_id=cat.id, name=cat.name, qty=q, cost=c, markup_pct=m,
                  price=svc.sale_price(c, m), picked_by=ctx.user.id,
                  wo_work_id=work.id if work and work.work_order_id == wo.id else None)
    ctx.db.add(part)
    ctx.db.flush()
    # Есть свободный остаток — резервируем, иначе заказываем (если не просили иначе)
    if action in ("auto", "stock") and svc.reserve_from_stock(ctx.db, ctx.branch.id, part):
        msg = f"«{cat.name}» — взято со склада в резерв"
    elif action in ("auto", "order"):
        svc.order_part(ctx.db, ctx.branch.id, part, _dec(order_qty) if order_qty else None)
        msg = f"«{cat.name}» — добавлено в закупку"
    else:
        msg = f"«{cat.name}» — добавлено, выберите: со склада или заказать"
    ctx.db.commit()
    flash(ctx.request, msg)
    return _to(f"/orders/{order_id}")


@router.post("/orders/{order_id}/parts/{part_id}/markup")
def part_markup(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx), markup_pct: str = Form(...),
                cost: str = Form("")):
    """Наценка по каждой позиции вручную (1–100 %). Цена продажи пересчитывается."""
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not part or part.work_order_id != wo.id or not ctx.can("prices.view_purchase"):
        return JSONResponse({"ok": False}, status_code=400)
    if cost.strip():
        part.cost = svc.money(_dec(cost))
    part.markup_pct = _markup(markup_pct, ctx)
    part.price = svc.sale_price(part.cost, part.markup_pct)
    ctx.db.commit()
    return {"ok": True, "price": str(part.price), "sum": str(svc.money(part.price * part.qty))}


@router.post("/orders/{order_id}/parts/{part_id}/delete")
def part_delete(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not _can_pick(ctx, wo) or not part or part.work_order_id != wo.id or part.status in ("issued", "installed"):
        raise Forbidden()
    if part.status == "reserved":
        svc.unreserve(ctx.db, part, "Позицию убрали из наряда")
    for line in ctx.db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id == part.id)):
        p = ctx.db.get(Purchase, line.purchase_id)
        if p.status == "draft":
            ctx.db.delete(line)
        else:
            line.wo_part_id = None  # уже заказано — придёт на свободный остаток
    ctx.db.flush()
    ctx.db.delete(part)
    ctx.db.commit()
    return _to(f"/orders/{order_id}")


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
    return ctx.render("stock/purchase.html", title="Закупка", p=p, lines=lines, cats=cats, parts=parts, orders=orders,
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
@router.get("/stock")
def stock_list(ctx: Ctx = Depends(get_ctx), q: str = "", only: str = ""):
    ctx.need("stock.view")
    db = ctx.db
    items = db.scalars(select(StockItem).where(StockItem.branch_id == ctx.branch.id)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.branch_id == ctx.branch.id))}
    locs = {l.id: l for l in db.scalars(select(Location).where(Location.branch_id == ctx.branch.id))}
    bal = svc.balances(db, ctx.branch.id)
    rows = []
    ql = q.strip().lower()
    for i in items:
        c = cats.get(i.catalog_id)
        if not c:
            continue
        if ql and ql not in f"{c.name} {c.article} {c.brand} {c.fits}".lower():
            continue
        b = bal[i.id]
        if only == "free" and b["free"] <= 0:
            continue
        if only != "all" and b["on_hand"] == 0 and b["reserved"] == 0:
            continue
        rows.append({"i": i, "c": c, "loc": svc.location_label(locs.get(i.location_id)), **b})
    rows.sort(key=lambda r: (r["c"].name.lower(), r["loc"]))
    new_requests = db.scalar(select(func.count()).select_from(IssueRequest)
                             .where(IssueRequest.branch_id == ctx.branch.id, IssueRequest.status == "new"))
    return ctx.render("stock/list.html", title="Склад", rows=rows, q=q, only=only, new_requests=new_requests,
                      n_locations=len(locs))


@router.get("/stock/items/{item_id}")
def stock_item(item_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("stock.view")
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    c = ctx.db.get(PartCatalog, i.catalog_id)
    moves = ctx.db.scalars(select(StockMovement).where(StockMovement.stock_item_id == i.id)
                           .order_by(StockMovement.created_at.desc()).limit(200)).all()
    orders = {o.id: o for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.id.in_([m.work_order_id for m in moves if m.work_order_id])))}
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_([m.created_by for m in moves if m.created_by])))}
    return ctx.render("stock/item.html", title=c.name, i=i, c=c, b=svc.balances(ctx.db, ctx.branch.id, [i.id])[i.id],
                      loc=svc.location_label(ctx.db.get(Location, i.location_id) if i.location_id else None),
                      moves=moves, orders=orders, people=people, MOVEMENT_TYPES=MOVEMENT_TYPES)


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
def pricing_save(ctx: Ctx = Depends(get_ctx), default_markup: str = Form("25"), partsman_margin_pct: str = Form("")):
    ctx.need("settings.access")
    try:
        st.put(ctx.db, ctx.branch.id, "pricing", {"default_markup": max(1, min(100, int(default_markup)))})
    except ValueError:
        pass
    # % запчастиста от наценки — только главный админ, 0–30
    if partsman_margin_pct.strip() and ctx.user.role.code == "admin":
        try:
            pct = float(partsman_margin_pct.replace(",", "."))
            st.put(ctx.db, ctx.branch.id, "payroll", {"partsman_margin_pct": max(0.0, min(30.0, pct))})
        except ValueError:
            pass
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _to("/settings/pricing")
