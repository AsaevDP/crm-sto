"""Шаг 5: подбор запчастей в наряд, закупки и приход, склад, запросы на выдачу."""
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select

from ..models import (IssueRequest, IssueRequestLine, Location, PartCatalog, Purchase, PurchaseLine, StockItem,
                      StockMovement, Supplier, User, Vehicle, WoPart, WorkOrder, WoWork)
from ..models.stock import CATEGORIES, MOVEMENT_TYPES, SECTIONS
from ..services import orders as osvc
from ..services import settings as st
from ..services import stock as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid

router = APIRouter()
PURCHASE_STATUS = {"draft": "Черновик", "ordered": "Ждём", "delivered": "Ждём",   # «delivered» — старое, см. fix_delivered
                   "partial": "Неполная поставка", "arrived": "Принята на склад"}
PURCHASE_CHIP = {"draft": "created", "ordered": "waiting", "delivered": "waiting", "partial": "picking", "arrived": "ready"}


def _safe_back(v: str, default: str) -> str:
    v = (v or "").strip()
    return v if v.startswith("/") and not v.startswith("//") else default


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


# Запчастист подбирает и во время ремонта: менеджер добавил деталь в «В работе» — её надо провести
# (отчёт тестировщика 11.11.2026: раньше отказ 403, деталь в счёте, а склад не тронут)
PICK_STATUSES = ("approve", "picking", "waiting", "instock", "work")   # 13.11.2026: подбор идёт уже на согласовании


def _can_pick(ctx: Ctx, wo: WorkOrder) -> bool:
    return (ctx.can("parts.pick") and wo.status in PICK_STATUSES and not wo.awaiting_prepay
            and (ctx.user.id == wo.partsman_id or ctx.can("orders.view_all")))


def _after_part_added(ctx: Ctx, wo: WorkOrder, part: WoPart):
    """Запчасти уже были на складе, а новую заказали — наряд снова «Ждёт запчасти» (если ремонт ещё не начат)."""
    if wo.status == "instock" and part.status in ("ordered", "need"):
        osvc.set_status(ctx.db, wo, "waiting", f"Добавлена запчасть «{part.name}» — ждём")


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
    if _dec(cost) < 0 or _dec(price) < 0:   # отрицательные цены не принимаем (отчёт тестировщика)
        flash(ctx.request, "Цена не может быть меньше нуля", "error")
        return _to(f"/orders/{order_id}#parts")
    if picker and _dec(cost) <= 0 and catalog_id:
        # 13.11.2026 (брат): деталь из каталога без закупки — закупка со склада (как подставляет окно подбора)
        si = ctx.db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id, StockItem.cost > 0)
                            .order_by(StockItem.updated_at.desc())).first()
        if si:
            cost = str(si.cost)
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
    elif wo.status in svc.PLAN_STATUSES:   # 13.11.2026: до согласования — резерв со склада или «заказать после согласования»
        msg = f"{label} — " + svc.plan_part(ctx.db, ctx.branch.id, part, action, _dec(order_qty) if order_qty else None)
    else:   # Есть свободный остаток — резервируем, иначе заказываем (если не просили иначе)
        if action in ("auto", "stock") and svc.reserve_from_stock(ctx.db, ctx.branch.id, part):
            msg = f"{label} — взято со склада в резерв"
        elif action in ("auto", "order"):
            svc.order_part(ctx.db, ctx.branch.id, part, _dec(order_qty) if order_qty else None)
            msg = f"{label} — добавлено в закупку"
        else:
            msg = f"{label} — добавлено, выберите: со склада или заказать"
    _after_part_added(ctx, wo, part)
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
    act = action if action in ("auto", "order") else "auto"
    msg = (svc.plan_part(ctx.db, ctx.branch.id, part, act) if wo.status in svc.PLAN_STATUSES
           else svc.source_part(ctx.db, ctx.branch.id, part, act))
    _after_part_added(ctx, wo, part)
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
    from ..services import money as msvc
    if wo.status in msvc.FROZEN:   # оплаченный / выданный наряд: цены уже не меняются (отчёт тестировщика 11.11.2026)
        return JSONResponse({"ok": False, "error": "Наряд оплачен или закрыт — цены менять нельзя"}, status_code=400)
    new_markup = _markup(markup_pct, ctx)
    # Цену клиенту назвал менеджер (закупки ещё нет): первая закупка не меняет цену — считаем наценку из неё
    keep_price = (cost.strip() and part.cost == 0 and part.price > 0 and new_markup == part.markup_pct)
    if cost.strip():
        part.cost = max(svc.money(_dec(cost)), Decimal(0))
    if keep_price and part.cost > 0:
        part.markup_pct = max(0, min(1000, int(round((part.price / part.cost - 1) * 100))))
    else:
        part.markup_pct = new_markup
        part.price = svc.sale_price(part.cost, part.markup_pct)
    ctx.db.flush()
    err = msvc.guard_total(ctx.db, wo)
    if err:
        ctx.db.rollback()
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    msvc.after_change(ctx.db, wo)
    ctx.db.commit()
    return {"ok": True, "price": str(part.price), "sum": str(svc.money(part.price * part.qty)), "markup": part.markup_pct}


ARCHIVE_STATUSES = ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready")


def _can_archive(ctx: Ctx, wo: WorkOrder) -> bool:
    """13.11.2026 (брат): убрать позицию в архив и вернуть из архива — менеджер (любую: ошибся с «Выбрать», клиент
    передумал, запчастист не дал выбора) и запчастист. Пока наряд не оплачен и не закрыт."""
    return wo.status in ARCHIVE_STATUSES and (ctx.can("orders.approve") or _can_pick(ctx, wo))


@router.post("/orders/{order_id}/parts/{part_id}/delete")
def part_delete(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    """«Убрать» → в архив наряда (13.11.2026): резерв снимается, черновая строка закупки убирается, позицию можно вернуть."""
    ctx.need("parts.pick", "orders.approve")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not _can_archive(ctx, wo) or not part or part.work_order_id != wo.id or part.status in ("returned", "archived"):
        raise Forbidden()
    err = svc.archive_part(ctx.db, part, ctx.user.id)
    if not err:
        from ..services import money as msvc
        err = msvc.guard_total(ctx.db, wo)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
        return _to(f"/orders/{order_id}#parts")
    svc.refresh_order_status(ctx.db, wo)   # убрали последнюю недостающую — «Запчасти на складе»
    ctx.db.commit()
    flash(ctx.request, f"«{part.name}» — в архиве наряда. Вернуть: «Архив» в блоке «Запчасти»")
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/parts/{part_id}/restore")
def part_restore(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    """Вернуть позицию из архива наряда (13.11.2026)."""
    ctx.need("parts.pick", "orders.approve")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    if not _can_archive(ctx, wo) or not part or part.work_order_id != wo.id or part.status != "archived":
        raise Forbidden()
    svc.restore_part(ctx.db, ctx.branch.id, wo, part)
    _after_part_added(ctx, wo, part)
    if wo.status == "ready" and part.status in ("need", "ordered", "reserved"):
        osvc.set_status(ctx.db, wo, "work", f"Вернули запчасть «{part.name}» — машина снова в ремонте")
    from ..services import money as msvc
    msvc.after_change(ctx.db, wo)
    ctx.db.commit()
    flash(ctx.request, f"«{part.name}» вернули в наряд")
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/parts/{part_id}/return")
def part_return(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    """Выданную слесарю запчасть вернули на склад (не подошла, клиент отказался). Кладовщик или запчастист."""
    ctx.need("stock.manage", "stock.issue", "parts.pick")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    from ..services import money as msvc
    if not part or part.work_order_id != wo.id or part.status not in ("issued", "installed") or wo.status in msvc.FROZEN:
        raise Forbidden()
    n = svc.return_issued(ctx.db, part, "Вернули на склад из наряда")
    part.status = "returned"
    ctx.db.flush()
    err = msvc.guard_total(ctx.db, wo)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
        return _to(f"/orders/{order_id}#parts")
    osvc.event(ctx.db, wo, "status", f"Запчасть «{part.name}» вернули на склад ({n.normalize():f} шт)")
    ctx.db.commit()
    flash(ctx.request, f"«{part.name}» — вернули на склад")
    return _to(f"/orders/{order_id}#parts")


@router.post("/orders/{order_id}/picking-done")
def picking_done(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("parts.pick")
    wo = _order(ctx, order_id)
    if wo.status == "approve" and _can_pick(ctx, wo):
        # 13.11.2026 (брат): подбор на согласовании — ничего не заказываем, менеджеру «Подбор готов»
        err = svc.mark_picked(ctx.db, wo, ctx.user)
        if err:
            ctx.db.rollback()
            flash(ctx.request, err, "error")
        else:
            ctx.db.commit()
            flash(ctx.request, "Подбор завершён — менеджер согласует с клиентом, после этого заказ придёт в «Мои заказы»")
    elif wo.status == "picking" and _can_pick(ctx, wo):
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
# 14.11.2026 (брат): шага «Привезли — ждут приёмки» нет. Всё заказанное — в «Ждём», пока кладовщик не примет:
# «✓ Принять полностью» или «Неполная поставка». Неполная — и в «Ждём» (остальное едет), и в «Принятых» (часть на складе).
PURCHASE_TABS = {"draft": ("Черновики", "gray", "собираем заказ"), "wait": ("Ждём", "orange", "заказано, едет"),
                 "partial": ("Неполная поставка", "purple", "часть принята, ждём остальное"),
                 "arrived": ("Приняты", "green", "на складе, и частично"), "all": ("Все", "blue", "за всё время")}
_TAB_STATUS = {"draft": ("draft",), "wait": ("ordered", "partial"), "partial": ("partial",), "arrived": ("arrived",)}


def _in_tab(p: Purchase, tab: str, got_any: set) -> bool:
    """14.11.2026 (брат): в «Принятых» — и накладная, где принята только часть (что бы с ней ни было дальше:
    ждём остальное или «довезли остальное»). Принятые детали видны сразу, не ждут остальных."""
    if tab == "all":
        return True
    return p.status in _TAB_STATUS[tab] or (tab == "arrived" and p.id in got_any)


@router.get("/purchases")
def purchases(ctx: Ctx = Depends(get_ctx), tab: str = "", sup: str = "", who: str = ""):
    ctx.need("purchases.view")
    db = ctx.db
    allp = db.scalars(select(Purchase).where(Purchase.branch_id == ctx.branch.id)
                      .order_by(Purchase.created_at.desc())).all()
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    buyers = sorted({p.created_by for p in allp if p.created_by in people}, key=lambda i: people[i].full_name)
    if who:   # 12.11.2026: закупки одного запчастиста
        allp = [p for p in allp if str(p.created_by) == who]
    got_any = set(db.scalars(select(PurchaseLine.purchase_id).join(Purchase, Purchase.id == PurchaseLine.purchase_id).where(
        Purchase.branch_id == ctx.branch.id, or_(PurchaseLine.received_qty > 0, PurchaseLine.received.is_(True)))))
    counts = {k: sum(_in_tab(p, k, got_any) for p in allp) for k in _TAB_STATUS}
    counts["all"] = len(allp)
    if tab not in PURCHASE_TABS:
        # кладовщику первым делом — что ждём (принять, когда привезут); остальным — черновики / что ждём
        tab = next((k for k in (("wait",) if ctx.can("stock.manage") else ()) + ("draft", "wait") if counts[k]), "all")
    rows = [p for p in allp if _in_tab(p, tab, got_any)]
    if sup:
        rows = [p for p in rows if str(p.supplier_id) == sup]
    rows = rows[:200]
    ids = [p.id for p in rows]
    stats = {}
    if ids:
        cat_names = {}
        for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id.in_(ids)).order_by(PurchaseLine.created_at)):
            s = stats.setdefault(l.purchase_id, {"n": 0, "total": Decimal(0), "orders": 0, "urgent": 0, "got": 0,
                                                 "got_names": [], "left_names": []})
            s["n"] += 1
            s["total"] += l.qty * l.price
            s["orders"] += 1 if l.wo_part_id else 0
            s["got"] += 1 if l.received else 0
            if l.catalog_id not in cat_names:
                c = db.get(PartCatalog, l.catalog_id)
                cat_names[l.catalog_id] = c.name if c else "позиция"
            nm = cat_names[l.catalog_id]
            # 14.11.2026: что принято и чего ждём — по каждой детали (для неполной поставки)
            if l.got > 0:
                s["got_names"].append(nm if l.received else f"{nm} ({l.got.normalize():f} из {l.qty.normalize():f})")
            if not l.received:
                s["left_names"].append(nm)
        urgent_parts = {x.id for x in db.scalars(select(WoPart).where(WoPart.urgent.is_(True)))}
        for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id.in_(ids), PurchaseLine.received.is_(False),
                                                       PurchaseLine.wo_part_id.in_(urgent_parts or [uuid.uuid4()]))):
            stats[l.purchase_id]["urgent"] += 1
    suppliers = db.scalars(select(Supplier).where(Supplier.branch_id == ctx.branch.id).order_by(Supplier.name)).all()
    p_orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([p.work_order_id for p in rows if p.work_order_id])))}
    p_veh = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in p_orders.values()])))}
    from datetime import date as _d
    return ctx.render("stock/purchases.html", title="Закупки", rows=rows, stats=stats,
                      people=people, buyers=[people[i] for i in buyers], who=who, p_orders=p_orders, p_veh=p_veh,
                      suppliers={s.id: s for s in suppliers}, supplier_list=suppliers, tab=tab, sup=sup,
                      counts=counts, TABS=PURCHASE_TABS, PURCHASE_STATUS=PURCHASE_STATUS, CHIP=PURCHASE_CHIP, today=_d.today())


def _orders_board(ctx: Ctx, uid, st: str, mode: str, people_tiles=None):
    rows = svc.partsman_lines(ctx.db, ctx.branch.id, uid)
    counts = svc.order_counts(rows)
    if st == "late":
        shown = [r for r in rows if r["late"]]
    elif st in svc.ORDER_STATES:
        shown = [r for r in rows if r["st"] == st]
    else:
        shown = [r for r in rows if r["st"] != "arrived"] or rows
    # 12.11.2026: строки одной накладной — рядом (у первой — кнопка «✓ Заказал» / приёмка). Сверху черновики
    # (ждут кнопки), потом опаздывающие, срочные; принятые — вниз.
    grp = {}
    for r in shown:
        g = grp.setdefault(r["p"].id, [False, False, False, True])
        g[0] |= r["st"] == "draft"
        g[1] |= r["late"]
        g[2] |= bool(r["part"] and r["part"].urgent)
        g[3] &= r["st"] == "arrived"
    order = {pid: i for i, pid in enumerate(dict.fromkeys(r["p"].id for r in shown))}
    shown.sort(key=lambda r: (not grp[r["p"].id][0], not grp[r["p"].id][1], not grp[r["p"].id][2], grp[r["p"].id][3],
                              order[r["p"].id]))
    n_lines = {}
    for r in shown:
        n_lines[r["p"].id] = n_lines.get(r["p"].id, 0) + 1
    # 13.11.2026 (брат): менеджер согласовал с клиентом — «на такую машину, такой наряд нужно заказать»: карточки сверху
    to_order = []
    if mode == "mine":
        for d in svc.draft_waits(ctx.db, ctx.branch.id, who=uid):
            wo = d["wo"]
            to_order.append({**d, "v": ctx.db.get(Vehicle, wo.vehicle_id) if wo else None,
                             "left": sum(1 for r in d["rows"] if not r["l"].ordered_at)})
    return dict(rows=shown[:300], counts=counts, st=st, mode=mode, people_tiles=people_tiles or [], n_lines=n_lines,
                STATES=svc.ORDER_STATES, fmt_plate=osvc.fmt_plate, cost_ok=ctx.can("prices.view_purchase"),
                to_order=to_order)


@router.get("/my-orders")
def my_orders(ctx: Ctx = Depends(get_ctx), st: str = ""):
    """Мои заказы (запчастист, 12.11.2026): всё, что он заказал по своим нарядам и на склад, и пришло ли."""
    ctx.need("purchases.mine")
    return ctx.render("stock/partsman_orders.html", title="Мои заказы", who=ctx.user,
                      **_orders_board(ctx, ctx.user.id, st, "mine"))


@router.get("/purchases/by-partsman")
def orders_by_partsman(ctx: Ctx = Depends(get_ctx), u: str = "", st: str = ""):
    """Заказы запчастистов (кладовщик, директор, админ): плитки по каждому, внутри — его заказы."""
    ctx.need("purchases.by_partsman")
    db = ctx.db
    all_rows = svc.partsman_lines(db, ctx.branch.id)
    staff = db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))).all()
    ids = {u_.id for u_ in staff if u_.role.code == "partsman"} | {r["buyer"].id for r in all_rows if r["buyer"]}
    tiles = []
    for p in sorted((x for x in db.scalars(select(User).where(User.id.in_(list(ids))))), key=lambda x: x.full_name):
        mine = [r for r in all_rows if r["buyer"] and r["buyer"].id == p.id]
        c = svc.order_counts(mine)
        tiles.append({"u": p, "wait": c["wait"] + c["partial"], "late": c["late"],
                      "arrived": c["arrived"], "draft": c["draft"]})
    who = db.get(User, _uuid(u)) if u else None
    if who and who.branch_id != ctx.branch.id:
        raise Forbidden()
    board = _orders_board(ctx, who.id, st, "all", tiles) if who else dict(
        rows=[], counts={}, st="", mode="all", people_tiles=tiles, STATES=svc.ORDER_STATES, fmt_plate=osvc.fmt_plate,
        cost_ok=ctx.can("prices.view_purchase"))
    return ctx.render("stock/partsman_orders.html", title="Заказы запчастистов", who=who, **board)


@router.post("/purchases/new")
def purchase_new(ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form("")):
    """Новая закупка у поставщика. Черновиков может быть сколько угодно — по одному на заказ."""
    ctx.need("purchases.manage")
    sid = _supplier_id(ctx, supplier_id, supplier_new)   # то же название — тот же поставщик (13.11.2026)
    p = Purchase(status="draft", supplier_id=sid)
    ctx.db.add(p)
    ctx.db.commit()
    return _to(f"/purchases/{p.id}")


def can_manage(ctx: Ctx, p: Purchase) -> bool:
    """Править закупку: запчастист (purchases.manage) — любую; менеджер / директор / зам — только закупку химии для мойки."""
    return ctx.can("purchases.manage") or (p.kind == "chem" and ctx.can("wash.buy"))


def _need_manage(ctx: Ctx, p: Purchase):
    if not can_manage(ctx, p):
        raise Forbidden()


def _purchase(ctx: Ctx, pid: str) -> Purchase:
    p = ctx.db.get(Purchase, _uuid(pid))
    if not p or p.branch_id != ctx.branch.id:
        raise Forbidden()
    return p


@router.get("/purchases/{pid}")
def purchase_view(pid: str, ctx: Ctx = Depends(get_ctx), short: str = ""):
    ctx.need("purchases.view")
    p = _purchase(ctx, pid)
    db = ctx.db
    lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id).order_by(PurchaseLine.created_at)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([l.catalog_id for l in lines])))}
    parts = {x.id: x for x in db.scalars(select(WoPart).where(WoPart.id.in_([l.wo_part_id for l in lines if l.wo_part_id])))}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_(
        [x.work_order_id for x in parts.values()] + ([p.work_order_id] if p.work_order_id else []))))}
    locations = db.scalars(select(Location).where(Location.branch_id == ctx.branch.id).order_by(Location.rack, Location.shelf, Location.place)).all()
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders.values()])))}
    drafts = db.scalars(select(Purchase).where(Purchase.branch_id == ctx.branch.id, Purchase.status == "draft",
                                               Purchase.id != p.id).order_by(Purchase.created_at)).all()
    sup_all = db.scalars(select(Supplier).where(Supplier.branch_id == ctx.branch.id).order_by(Supplier.name)).all()
    from ..services import brands as _br
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("stock/purchase.html", title="Закупка химии" if p.kind == "chem" else "Закупка", p=p, lines=lines,
                      can_manage=can_manage(ctx, p), cats=cats, parts=parts, orders=orders,
                      buyer=people.get(p.created_by), people=people, short=bool(short),
                      vehicles=vehicles, BRANDS=_br.names(db, ctx.branch.id), suppliers=sup_all,
                      sup_names={s.id: s.name for s in sup_all}, drafts=drafts,
                      supplier=db.get(Supplier, p.supplier_id) if p.supplier_id else None,
                      locations=locations, loc_label=svc.location_label, PURCHASE_STATUS=PURCHASE_STATUS, CHIP=PURCHASE_CHIP,
                      cells={l.id: svc.default_location(db, l.catalog_id) for l in lines if not l.received},
                      fmt_plate=osvc.fmt_plate, total=sum((l.qty * l.price for l in lines), Decimal(0)),
                      total_got=sum((l.got * l.price for l in lines), Decimal(0)))


@router.post("/purchases/{pid}/ordered")
def purchase_ordered(pid: str, ctx: Ctx = Depends(get_ctx), back: str = Form("")):
    """«✓ Заказал» (12.11.2026) — из «Моих заказов», у каждого черновика. Поставщик, счёт, дата не трогаются."""
    p = _purchase(ctx, pid)
    _need_manage(ctx, p)
    db = ctx.db
    to = back if back.startswith("/my-orders") else f"/purchases/{pid}"
    if p.status != "draft":
        flash(ctx.request, "Эта закупка уже заказана")
        return _to(to)
    if not db.scalar(select(func.count()).select_from(PurchaseLine).where(PurchaseLine.purchase_id == p.id)):
        flash(ctx.request, "В закупке нет строк", "error")
        return _to(to)
    if not p.supplier_id:   # 13.11.2026 (брат): без поставщика кладовщик не поймёт, от кого накладная
        flash(ctx.request, "Выберите, у кого заказали — без поставщика «Заказал» нельзя", "error")
        return _to(to)
    from datetime import date as _d
    from ..db import now
    p.status, p.ordered_on = "ordered", _d.today()
    for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id, PurchaseLine.ordered_at.is_(None))):
        l.ordered_at, l.ordered_by = now(), ctx.user.id
    db.commit()
    flash(ctx.request, "Заказано — накладная ушла кладовщику в «Приход»")
    return _to(to)


def _supplier_id(ctx: Ctx, supplier_id: str, supplier_new: str):
    """Поставщик из формы: выбранный из списка или новый (вписали название). None — не выбрали."""
    if supplier_new.strip():
        name = supplier_new.strip()[:160]
        s = ctx.db.scalar(select(Supplier).where(Supplier.branch_id == ctx.branch.id, func.lower(Supplier.name) == name.lower()))
        if not s:
            s = Supplier(name=name)
            ctx.db.add(s)
            ctx.db.flush()
        return s.id
    s = ctx.db.get(Supplier, _uuid(supplier_id)) if supplier_id else None
    return s.id if s and s.branch_id == ctx.branch.id else None


def _line(ctx: Ctx, lid: str) -> tuple[PurchaseLine, Purchase]:
    line = ctx.db.get(PurchaseLine, _uuid(lid))
    if not line:
        raise Forbidden()
    p = _purchase(ctx, str(line.purchase_id))
    _need_manage(ctx, p)
    return line, p


@router.post("/purchase-lines/{lid}/ordered")
def purchase_line_ordered(lid: str, ctx: Ctx = Depends(get_ctx), back: str = Form(""), expected_on: str = Form("")):
    """«Заказал» по одной детали (13.11.2026, брат): она сразу — в накладную у кладовщика в «Приходе».
    Без поставщика — нельзя. Дата «привезут» — одна на всех деталей этого поставщика в наряде."""
    db = ctx.db
    line, p = _line(ctx, lid)
    # черновик после переноса последней строки удаляется — возвращаемся в «Заказ по наряду» или в накладную
    to = back if back.startswith("/my-orders") else (f"/my-orders/wo/{p.work_order_id}" if p.work_order_id else f"/purchases/{p.id}")
    if p.status != "draft":
        return _to(to)
    cat = db.get(PartCatalog, line.catalog_id)
    target = svc.order_line(db, line, ctx.user.id, _date(expected_on))
    if target is None:
        flash(ctx.request, f"«{cat.name if cat else 'Деталь'}»: выберите, у кого заказали — без поставщика «Заказал» нельзя", "error")
        return _to(to)
    db.commit()
    if not p.work_order_id and not back.startswith("/my-orders"):
        to = f"/purchases/{target.id}"
    sup = db.get(Supplier, target.supplier_id)
    flash(ctx.request, f"«{cat.name if cat else 'Деталь'}» — заказано у {sup.name if sup else 'поставщика'}, кладовщик видит в «Приходе»")
    return _to(to)


@router.post("/purchase-lines/{lid}/supplier")
def purchase_line_supplier(lid: str, ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form(""),
                           back: str = Form("")):
    """Поставщик у одной детали черновика (13.11.2026, брат)."""
    db = ctx.db
    line, p = _line(ctx, lid)
    to = back if back.startswith("/my-orders") else (f"/my-orders/wo/{p.work_order_id}" if p.work_order_id else None)
    sid = _supplier_id(ctx, supplier_id, supplier_new)
    target = p
    if p.status == "draft" and sid:
        target = svc.set_line_supplier(db, line, sid)
        db.commit()
    return _to(to or f"/purchases/{target.id}")


def _wo_for_order(ctx: Ctx, wo_id: str) -> WorkOrder:
    wo = ctx.db.get(WorkOrder, _uuid(wo_id))
    if not wo or wo.branch_id != ctx.branch.id:
        raise Forbidden()
    return wo


@router.get("/my-orders/wo/{wo_id}")
def order_by_wo(wo_id: str, ctx: Ctx = Depends(get_ctx)):
    """«Заказ по наряду» (13.11.2026, брат): все детали машины — у каждой свой поставщик и своя кнопка «Заказал».
    Сверху — «Один поставщик на весь наряд». Ниже — что уже заказано, по поставщикам, и пришло ли."""
    if not (ctx.can("purchases.mine") or ctx.can("purchases.view")):
        raise Forbidden()
    db = ctx.db
    wo = _wo_for_order(ctx, wo_id)
    all_sups = db.scalars(select(Supplier).where(Supplier.branch_id == ctx.branch.id).order_by(Supplier.name, Supplier.created_at)).all()
    sup_by = {s.id: s for s in all_sups}
    sups = list({s.name.strip().lower(): s for s in reversed(all_sups)}.values())[::-1]   # в списке каждое имя — один раз
    sups.sort(key=lambda s: s.name.lower())
    todo = []
    for l in svc.order_drafts_of(db, ctx.branch.id, wo.id):
        p = db.get(Purchase, l.purchase_id)
        part = db.get(WoPart, l.wo_part_id) if l.wo_part_id else None
        todo.append({"l": l, "p": p, "c": db.get(PartCatalog, l.catalog_id), "part": part, "sup": sup_by.get(p.supplier_id)})
    done = {}
    for l, p in db.execute(select(PurchaseLine, Purchase).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                           .where(Purchase.branch_id == ctx.branch.id, Purchase.work_order_id == wo.id, Purchase.status != "draft")
                           .order_by(Purchase.created_at, PurchaseLine.created_at)):
        g = done.setdefault(p.supplier_id, {"sup": sup_by.get(p.supplier_id), "rows": [], "ps": []})
        if p not in g["ps"]:
            g["ps"].append(p)
        g["rows"].append({"l": l, "p": p, "c": db.get(PartCatalog, l.catalog_id),
                          "part": db.get(WoPart, l.wo_part_id) if l.wo_part_id else None})
    from datetime import date as _d
    return ctx.render("stock/order_wo.html", title=f"Заказ · {wo.number}", wo=wo, v=db.get(Vehicle, wo.vehicle_id),
                      todo=todo, done=list(done.values()), suppliers=sups, fmt_plate=osvc.fmt_plate,
                      can_order=ctx.can("purchases.manage"), cost_ok=ctx.can("prices.view_purchase"),
                      PURCHASE_STATUS=PURCHASE_STATUS, today=_d.today())


@router.post("/my-orders/wo/{wo_id}/supplier")
def order_by_wo_supplier(wo_id: str, ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form("")):
    """«Один поставщик на весь наряд»: подставляется во все ещё не заказанные детали (и меняет выбранных ранее)."""
    ctx.need("purchases.manage")
    db = ctx.db
    wo = _wo_for_order(ctx, wo_id)
    sid = _supplier_id(ctx, supplier_id, supplier_new)
    if not sid:
        flash(ctx.request, "Выберите поставщика из списка или впишите нового", "error")
        return _to(f"/my-orders/wo/{wo.id}")
    lines = svc.order_drafts_of(db, ctx.branch.id, wo.id)
    for p in svc.wo_drafts(db, ctx.branch.id, wo.id):
        svc.set_draft_supplier(db, p, sid)
    db.commit()
    flash(ctx.request, f"Поставщик «{db.get(Supplier, sid).name}» — у всех деталей: {len(lines)}")
    return _to(f"/my-orders/wo/{wo.id}")


@router.post("/my-orders/wo/{wo_id}/ordered")
def order_by_wo_all(wo_id: str, ctx: Ctx = Depends(get_ctx), expected_on: str = Form(""), back: str = Form("")):
    """«✓ Заказал всё» по наряду: каждая деталь — в накладную своего поставщика. Если у какой-то нет поставщика —
    ничего не заказываем и говорим, у какой."""
    ctx.need("purchases.manage")
    db = ctx.db
    wo = _wo_for_order(ctx, wo_id)
    to = back if back.startswith("/my-orders") else f"/my-orders/wo/{wo.id}"
    lines = svc.order_drafts_of(db, ctx.branch.id, wo.id)
    nosup = [l for l in lines if not db.get(Purchase, l.purchase_id).supplier_id]
    if nosup:
        names = ", ".join((db.get(PartCatalog, l.catalog_id).name for l in nosup[:3]))
        flash(ctx.request, f"Не выбран поставщик: {names}{'…' if len(nosup) > 3 else ''} — выберите, у кого заказали", "error")
        return _to(f"/my-orders/wo/{wo.id}")
    sups = set()
    for p in svc.wo_drafts(db, ctx.branch.id, wo.id):
        t = svc.order_draft(db, p, ctx.user.id, _date(expected_on))
        sups.add(t.supplier_id)
    db.commit()
    flash(ctx.request, f"Заказано: {len(lines)} поз. у поставщиков: {len(sups)} — кладовщик видит в «Приходе»" if lines
          else "Заказывать нечего — всё уже заказано")
    return _to(to)


def _date(v: str):
    from datetime import date as _d
    try:
        return _d.fromisoformat(v) if v else None
    except ValueError:
        return None


@router.post("/purchases/{pid}")
def purchase_save(pid: str, ctx: Ctx = Depends(get_ctx), supplier_id: str = Form(""), supplier_new: str = Form(""),
                  supplier_invoice: str = Form(""), note: str = Form(""), action: str = Form("save"),
                  expected_on: str = Form(""), supplier_phone: str = Form("")):
    p = _purchase(ctx, pid)
    _need_manage(ctx, p)
    db = ctx.db
    if action == "delete":
        if p.status != "draft":
            raise Forbidden()
        svc.delete_purchase(db, p)
        db.commit()
        flash(ctx.request, "Черновик закупки удалён. Позиции нарядов из него снова ждут решения запчастиста")
        return _to("/purchases")
    if action == "back_to_draft":   # отправили по ошибке — пока ничего не пришло, можно вернуть в черновик
        if p.status == "ordered":
            p.status = "draft"
            flash(ctx.request, "Закупка снова черновик — можно править")
        db.commit()
        return _to(f"/purchases/{pid}")
    if supplier_new.strip():   # то же название — тот же поставщик, дубли не плодим (13.11.2026)
        p.supplier_id = _supplier_id(ctx, "", supplier_new)
        s = db.get(Supplier, p.supplier_id)
        if supplier_phone.strip():
            s.phone = supplier_phone.strip()[:40]
    elif supplier_id:
        s = db.get(Supplier, _uuid(supplier_id))
        p.supplier_id = s.id if s and s.branch_id == ctx.branch.id else None
        if s and supplier_phone.strip():
            s.phone = supplier_phone.strip()[:40]
    p.supplier_invoice, p.note = supplier_invoice.strip()[:80], note.strip()
    p.expected_on = _date(expected_on)
    if action == "ordered" and p.status == "draft":
        has_lines = db.scalar(select(func.count()).select_from(PurchaseLine).where(PurchaseLine.purchase_id == p.id))
        if not has_lines:
            flash(ctx.request, "В закупке нет строк", "error")
            return _to(f"/purchases/{pid}")
        if not p.supplier_id:
            db.commit()
            flash(ctx.request, "Выберите, у кого заказали — без поставщика «Заказан» нельзя", "error")
            return _to(f"/purchases/{pid}")
        from datetime import date as _d
        p.status, p.ordered_on = "ordered", _d.today()
        flash(ctx.request, "Заказ отправлен поставщику — строки появились у кладовщика в «Приходе»")
    else:
        flash(ctx.request, "Сохранено")
    db.commit()
    return _to(f"/purchases/{pid}")


def _add_line(ctx: Ctx, p: Purchase, name, article, brand, qty, price, catalog_id="") -> bool:
    from ..services import brands
    cat = ctx.db.get(PartCatalog, _uuid(catalog_id)) if catalog_id else None
    if cat is None:
        if not (name or "").strip():
            return False
        brand = brands.norm(brand)
        if brand:
            brands.add(ctx.db, ctx.branch.id, brand)
        cat = svc.get_or_create_catalog(ctx.db, name, article, brand)
    if p.kind == "chem":   # 12.11.2026: закупка для мойки — позиция в разделе «Химия»
        from ..models.stock import guess_category
        cat.section, cat.category = "chem", (cat.category if cat.section == "chem" else "") or guess_category(cat.name, "chem")
    ctx.db.add(PurchaseLine(purchase_id=p.id, catalog_id=cat.id, qty=max(Decimal(qty), Decimal("0.01")),
                            price=svc.money(price), received_qty=0))
    return True


def _editable(p: Purchase) -> bool:
    """Строки можно добавлять/править в черновике и в заказе, пока он едет (поставщик что-то поменял)."""
    return p.status in ("draft", *svc.OPEN)


@router.post("/purchases/{pid}/lines")
def purchase_line_add(pid: str, ctx: Ctx = Depends(get_ctx), catalog_id: str = Form(""), name: str = Form(""),
                      article: str = Form(""), brand: str = Form(""), qty: str = Form("1"), price: str = Form("0"),
                      paste: str = Form("")):
    """Строка «на склад» (впрок, без наряда) или сразу список — вставкой из Excel / счёта поставщика."""
    p = _purchase(ctx, pid)
    _need_manage(ctx, p)
    if not _editable(p):
        raise Forbidden()
    if paste.strip():
        rows = svc.parse_paste(paste)
        for r in rows:
            _add_line(ctx, p, r["name"], r["article"], r["brand"], r["qty"], r["price"])
        flash(ctx.request, f"Добавлено строк из списка: {len(rows)}. Проверьте названия и цены" if rows
              else "Не понял список: нужна строка «Название; Артикул; Бренд; Кол-во; Цена»", "ok" if rows else "error")
    elif not _add_line(ctx, p, name, article, brand, max(_dec(qty, "1"), Decimal("0.01")), _dec(price), catalog_id):
        flash(ctx.request, "Название обязательно", "error")
    svc.refresh_purchase_status(ctx.db, p)
    ctx.db.commit()
    return _to(f"/purchases/{pid}#lines")


@router.post("/purchases/{pid}/lines/{lid}")
def purchase_line_edit(pid: str, lid: str, ctx: Ctx = Depends(get_ctx), qty: str = Form(""), price: str = Form(""),
                       delete: str = Form(""), action: str = Form("save"), target: str = Form("")):
    p = _purchase(ctx, pid)
    _need_manage(ctx, p)
    db = ctx.db
    line = db.get(PurchaseLine, _uuid(lid))
    if not _editable(p) or not line or line.purchase_id != p.id:
        raise Forbidden()
    part = db.get(WoPart, line.wo_part_id) if line.wo_part_id else None
    if delete:
        action = "delete"
    tgt, msg = None, ""
    if action in ("move", "rest"):
        if target == "new":
            tgt = Purchase(status="draft", supplier_id=None)
            db.add(tgt)
            db.flush()
        else:
            tgt = db.get(Purchase, _uuid(target)) if target else None
            if not tgt or tgt.branch_id != ctx.branch.id or tgt.status != "draft" or tgt.id == p.id:
                flash(ctx.request, "Выберите черновик, куда перенести", "error")
                return _to(f"/purchases/{pid}#lines")
    if action == "delete":
        if line.got > 0:
            flash(ctx.request, "Часть уже пришла — строку удалить нельзя. Остаток можно перенести в другую закупку", "error")
            return _to(f"/purchases/{pid}#lines")
        svc.unlink_line(db, line)
        db.delete(line)
        msg = "Строка убрана" + (" — позиция наряда снова ждёт решения запчастиста" if part else "")
    elif action == "move":   # целиком в другой черновик (другой поставщик)
        if line.got > 0:
            action = "rest"
        else:
            line.purchase_id = tgt.id
            msg = "Строка перенесена в другую закупку"
    if action == "rest":   # пришло не всё — остаток в другую закупку
        svc.move_rest(db, ctx.branch.id, line, tgt)
        msg = "Остаток перенесён в другую закупку"
    elif action == "save":
        floor = max(line.got, Decimal("0.01"))
        only_line = part and not db.scalar(select(func.count()).select_from(PurchaseLine).where(
            PurchaseLine.wo_part_id == part.id, PurchaseLine.id != line.id))
        if only_line and part.status == "ordered":   # под наряд — не меньше, чем нужно наряду
            floor = max(floor, Decimal(part.qty))
        new_qty = _dec(qty, str(line.qty))
        if new_qty < floor:
            flash(ctx.request, f"Меньше {floor.normalize():f} нельзя: столько нужно наряду или уже пришло", "error")
            new_qty = floor
        line.qty = new_qty
        line.received = line.got >= line.qty
        if price.strip():
            line.price = max(svc.money(_dec(price)), Decimal(0))
            wo_ = db.get(WorkOrder, part.work_order_id) if part else None
            # Оплаченный / закрытый наряд цену клиенту не меняет (отчёт тестировщика 11.11.2026)
            if (part and part.cost != line.price and part.status in ("need", "ordered")
                    and wo_ and wo_.status not in ("ready", "paid", "closed", "cancel")):
                part.cost = line.price
                part.price = svc.sale_price(part.cost, part.markup_pct)
        msg = "Сохранено"
    db.flush()
    svc.refresh_purchase_status(db, p)
    if tgt is not None:
        svc.refresh_purchase_status(db, tgt)
    db.commit()
    flash(ctx.request, msg)
    return _to(f"/purchases/{pid}#lines")


@router.post("/purchases/{pid}/receive")
async def purchase_receive(pid: str, ctx: Ctx = Depends(get_ctx)):
    """Приход из карточки закупки: строка (сколько пришло, в какую ячейку) или «Принять всё»."""
    ctx.need("stock.manage")
    p = _purchase(ctx, pid)
    if p.status not in svc.OPEN:
        raise Forbidden()
    form = dict(await ctx.request.form())
    if not form.get("line_id"):
        form["all"] = str(p.id)   # без строки — «Принять всё» по этой закупке
    n, ready = _receive(ctx, form, [p])
    np_ = ctx.request.state.no_price
    ni_ = ctx.request.state.no_inv
    if ni_ and not n:
        flash(ctx.request, "Впишите № накладной — без него на склад не принимаем", "error")
        return _to(_safe_back(str(form.get("back", "")), f"/purchases/{pid}#lines"))
    if form.get("short") and not np_:
        # 14.11.2026 (брат): «Неполная поставка → Принять привезённое» — приёмка этой поставки закончена.
        # Чего не привезли (вписали 0 или меньше) — ждём дальше: накладная «неполная поставка» (если что-то принято)
        # или «Ждём» (не привезли ничего).
        p = ctx.db.get(Purchase, p.id)
        svc.refresh_purchase_status(ctx.db, p)
        ctx.db.commit()
        if p.status in ("partial", "ordered"):
            wait = [ctx.db.get(PartCatalog, l.catalog_id).name for l in ctx.db.scalars(
                select(PurchaseLine).where(PurchaseLine.purchase_id == p.id, PurchaseLine.received.is_(False)))]
            flash(ctx.request, (f"Принято: {n}. " if n else "Ничего не принято. ") + "Неполная поставка — ждём: " + ", ".join(wait[:5])
                  + (f". Все запчасти на складе: {', '.join(ready)}" if ready else ""))
            return _to(_safe_back(str(form.get("back", "")), f"/purchases/{pid}#lines"))
    left = ctx.db.get(Purchase, p.id).status == "partial"
    flash(ctx.request, (f"Принято строк: {n}" if n else "Ничего не принято — впишите, сколько привезли" if not np_ else "Ничего не принято")
          + (". Неполная поставка — остальное ждём" if n and left else "")
          + (f". Без № накладной не принято: {', '.join(ni_[:5])}" if ni_ else "")
          + (f". Все запчасти на складе: {', '.join(ready)}" if ready else "")
          + (f". Без цены не принято: {', '.join(np_[:5])} — впишите «₴ за шт» из накладной" if np_ else ""),
          "ok" if n and not np_ else "error")
    return _to(_safe_back(str(form.get("back", "")), f"/purchases/{pid}#lines"))


@router.post("/purchases/{pid}/delivered")
def purchase_delivered(pid: str, ctx: Ctx = Depends(get_ctx), back: str = Form(""), invoice: str = Form("")):
    """14.11.2026: шага «Привезли» больше нет — адрес оставлен для старых ссылок: записывает только № накладной."""
    ctx.need("stock.manage")
    p = _purchase(ctx, pid)
    if svc.mark_delivered(ctx.db, p, ctx.user, invoice):
        ctx.db.commit()
    return _to(_safe_back(back, f"/purchases/{pid}"))


def _receive(ctx: Ctx, form, purchases) -> tuple[int, list]:
    """Общий приход для карточки закупки и экрана «Приход». В форме: line_id (одна строка) или all (вся закупка),
    loc_<id> — ячейка, qty_<id> — сколько пришло (пусто — всё, что ждём)."""
    db = ctx.db
    ids = {p.id for p in purchases}
    if form.get("line_id"):
        lines = [db.get(PurchaseLine, _uuid(str(form["line_id"])))]
    elif form.get("sup"):   # 13.11.2026: «Принять всё» у поставщика — все его накладные (who — только этого запчастиста)
        key, who = str(form["sup"]), str(form.get("who", ""))
        ps = [p for p in purchases if _sup_key(db.get(Supplier, p.supplier_id) if p.supplier_id else None) == key
              and (not who or str(p.created_by) == who)]
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id.in_([p.id for p in ps] or [uuid.uuid4()]),
                                                      PurchaseLine.received.is_(False))).all()
    elif form.get("all"):
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == _uuid(str(form["all"])),
                                                      PurchaseLine.received.is_(False))).all()
    else:
        lines = []
    done, ready, item_ids, touched = 0, [], [], set()
    got_parts, before = [], {}   # 12.11.2026: что пришло под наряды — одно сообщение на приёмку (svc.arrived_notify)
    no_price = ctx.request.state.no_price = []   # 13.11.2026: без цены из накладной — не принято
    no_inv = ctx.request.state.no_inv = []       # 14.11.2026: без № накладной — не принято
    for l in lines:
        p = db.get(Purchase, l.purchase_id) if l else None
        if not l or l.received or not p or p.id not in ids or p.status not in svc.OPEN:
            continue
        # 14.11.2026 (брат): № накладной обязателен — в строке (inv_<id>) или общий у «Принять всё» (invoice)
        inv = str(form.get(f"inv_{l.id}", "") or form.get("invoice", "")).strip()[:80]
        if not inv:
            cat = db.get(PartCatalog, l.catalog_id)
            no_inv.append(cat.name if cat else "позиция")
            continue
        wp = db.get(WoPart, l.wo_part_id) if l.wo_part_id else None
        if wp and wp.work_order_id not in before:
            before[wp.work_order_id] = db.get(WorkOrder, wp.work_order_id).status
        was = wp.status if wp else None
        # 13.11.2026 (брат): цена обязательна — кладовщик переписывает её из накладной («₴ за шт»). Без цены не принимаем.
        pr = str(form.get(f"price_{l.id}", "")).strip()
        if pr and _dec(pr) > 0:
            l.price = svc.money(_dec(pr))
            if wp and (wp.cost or 0) <= 0 and wp.status not in ("issued", "installed"):
                wp.cost = l.price   # у позиции наряда не было закупки — берём из накладной, цена клиенту по наценке
                if (wp.price or 0) <= 0:
                    wp.price = svc.sale_price(wp.cost, wp.markup_pct or 0)
        if (l.price or 0) <= 0:
            cat = db.get(PartCatalog, l.catalog_id)
            no_price.append(cat.name if cat else "позиция")
            continue
        raw = str(form.get(f"loc_{l.id}", "") or form.get("location_id", ""))
        loc = db.get(Location, _uuid(raw)) if raw else None
        q = str(form.get(f"qty_{l.id}", "")).strip()
        took = svc.receive_line(db, ctx.branch.id, l, loc.id if loc and loc.branch_id == ctx.branch.id else None,
                                _dec(q) if q else None)
        if took <= 0:
            continue
        have = [x.strip() for x in (l.invoice or "").split(",") if x.strip()]
        if inv not in have:   # пришла частями по разным накладным — номера через запятую
            l.invoice = ", ".join(have + [inv])[:160]
        db.flush()
        item = db.scalar(select(StockItem).where(StockItem.catalog_id == l.catalog_id,
                                                 StockItem.location_id == l.location_id if l.location_id else StockItem.location_id.is_(None)))
        if item:
            item_ids.append(str(item.id))
        if l.wo_part_id:
            wo = db.get(WorkOrder, db.get(WoPart, l.wo_part_id).work_order_id)
            if wo.status == "instock" and wo.number not in ready:
                ready.append(wo.number)
            if wp and was != "reserved" and wp.status == "reserved" and wp not in got_parts:
                got_parts.append(wp)
        touched.add(p.id)
        done += 1
    for pid in touched:
        pp = db.get(Purchase, pid)
        svc.refresh_purchase_status(db, pp)
        # номер(а) накладных у закупки — из строк (для списков «счёт …»)
        nums = []
        for x in db.scalars(select(PurchaseLine.invoice).where(PurchaseLine.purchase_id == pid)):
            nums += [y.strip() for y in (x or "").split(",") if y.strip() and y.strip() not in nums]
        if nums:
            pp.supplier_invoice = ", ".join(nums)[:80]
    svc.arrived_notify(db, got_parts, before)
    db.commit()
    if done:
        ctx.request.session["last_received"] = {"n": done, "ids": ",".join(dict.fromkeys(item_ids)), "ready": ready}
    return done, ready


# ======================= Склад =======================
def _stock_rows(ctx: Ctx, q: str, only: str, sec: str, cat: str, sub: str, make: str, model: str):
    """Строки склада с фильтрами. Б/у (разбор) здесь не показываются — они в разделе «Разбор»."""
    from ..services import stock_cat as sc
    db = ctx.db
    items = db.scalars(select(StockItem).where(StockItem.branch_id == ctx.branch.id)).all()
    used = sec == "used"   # 11.11.2026: слесарю — вкладка «Разбор (б/у)», только наличие, без цен
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.branch_id == ctx.branch.id,
                                                                  (PartCatalog.kind == "used") if used else (PartCatalog.kind != "used")))}
    locs = {l.id: l for l in db.scalars(select(Location).where(Location.branch_id == ctx.branch.id))}
    fits = sc.fits_map(db, cats.keys())
    bal = svc.balances(db, ctx.branch.id)
    rows, counts = [], {}
    ql = q.strip().lower()
    for i in items:
        c = cats.get(i.catalog_id)
        if not c or (not used and (c.section or "repair") != sec):
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
               make: str = "", model: str = "", order: str = "", donor: str = ""):
    """Склад (10.11.2026): вкладки «ТО» и «Запчасти», дерево категорий, фильтр по марке и модели, штрихкоды.
    Слесарь (stock.look, 11.11.2026) — только смотрит: название, наличие, количество; ещё вкладка «Разбор (б/у)»."""
    ctx.need("stock.view", "stock.look")
    from ..services import stock_cat as sc
    db = ctx.db
    look = not ctx.can("stock.view")
    sec = sec if sec in SECTIONS or (sec == "used" and look) else "to"
    hit = sc.by_barcode(db, ctx.branch.id, q)   # сканер «напечатал» штрихкод — сразу в карточку
    if hit:
        return _to(f"/stock/items/{hit.id}")
    sc.seed(db, ctx.branch.id)
    db.commit()
    order = order if osvc._uuid(order) else ""
    rows, counts = _stock_rows(ctx, q, only, sec, cat, sub, make, model)
    if look:
        return _look(ctx, q, sec, cat, sub, make, model, order, donor)
    new_requests = db.scalar(select(func.count()).select_from(IssueRequest)
                             .where(IssueRequest.branch_id == ctx.branch.id, IssueRequest.status == "new"))
    if ctx.can("tools.manage"):   # + запросы по инструменту (11.11.2026)
        from ..models import ToolRequest
        new_requests = (new_requests or 0) + (db.scalar(select(func.count()).select_from(ToolRequest).where(
            ToolRequest.branch_id == ctx.branch.id, ToolRequest.status == "new")) or 0)
    n_locations = db.scalar(select(func.count()).select_from(Location).where(Location.branch_id == ctx.branch.id))
    mm = sc.makes_models(db, ctx.branch.id)
    # Плитки как на «Пульте» (11.11.2026): позиций в разделах, в резерве под наряды, сумма склада (по закупке)
    other = {s: (rows if s == sec and not (q or only or cat or make) else _stock_rows(ctx, "", "", s, "", "", "", "")[0])
             for s in SECTIONS}
    tiles = {"sec": {s: len(v) for s, v in other.items()},
             "reserved": sum(1 for v in other.values() for r in v if r["reserved"] > 0),
             "sum": sum((r["i"].cost * max(r["on_hand"], 0) for v in other.values() for r in v), Decimal(0))}
    return ctx.render("stock/list.html", title="Склад", rows=rows, q=q, only=only, new_requests=new_requests, tiles=tiles,
                      SECTIONS=SECTIONS, pick_orders=_pick_orders(ctx),
                      to_receive=_receive_count(ctx) if ctx.can("stock.manage") else 0,
                      sec=sec, cat=cat, sub=sub, make=make, model=model, tree=sc.tree(db, ctx.branch.id, sec),
                      counts=counts, makes=mm, models=mm.get(make, []), n_locations=n_locations, fit_label=sc.fit_label,
                      args=lambda **o: _stock_args(**{**dict(sec=sec, cat=cat, sub=sub, make=make, model=model, q=q, only=only), **o}))


def _pick_orders(ctx: Ctx) -> list[dict]:
    """Открытые наряды запчастиста, куда можно добавить со склада (12.11.2026): номер, машина, этап."""
    if not ctx.can("parts.pick"):
        return []
    q = select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, WorkOrder.status.in_(PICK_STATUSES),
                                WorkOrder.awaiting_prepay.is_not(True))
    if not ctx.can("orders.view_all") or ctx.user.role.code == "partsman":
        q = q.where(WorkOrder.partsman_id == ctx.user.id)
    orders = ctx.db.scalars(q.order_by(WorkOrder.created_at.desc()).limit(40)).all()
    vs = {v.id: v for v in ctx.db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))}
    from ..models.orders import WO_STATUSES
    return [{"id": o.id, "label": f"{o.number[-6:]} · {vs[o.vehicle_id].make} {vs[o.vehicle_id].model} {osvc.fmt_plate(vs[o.vehicle_id].plate)}"
             f" · {WO_STATUSES.get(o.status, o.status).lower()}" if o.vehicle_id in vs else o.number} for o in orders]


@router.post("/stock/items/{item_id}/to-order")
def stock_to_order(item_id: str, ctx: Ctx = Depends(get_ctx), order_id: str = Form(""), qty: str = Form("1"),
                   back: str = Form("")):
    """Склад → «В наряд» (12.11.2026): запчастист выбрал позицию на складе и свой открытый наряд — сразу в резерв наряда."""
    ctx.need("parts.pick")
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    to = _safe_back(back, "/stock")
    if not order_id:
        flash(ctx.request, "Выберите наряд", "error")
        return _to(to)
    wo = _order(ctx, order_id)
    if not _can_pick(ctx, wo):
        raise Forbidden()
    cat = ctx.db.get(PartCatalog, i.catalog_id)
    n = max(_dec(qty, "1"), Decimal("0.01"))
    free = svc.catalog_free(ctx.db, ctx.branch.id, [cat.id]).get(cat.id, Decimal(0))
    if n > free:
        flash(ctx.request, f"Свободно только {free.normalize():f} — столько в наряд не поставить", "error")
        return _to(to)
    m = _markup("", ctx)
    part = WoPart(work_order_id=wo.id, catalog_id=cat.id, name=cat.name, qty=n, cost=svc.money(i.cost or 0), markup_pct=m,
                  price=svc.sale_price(svc.money(i.cost or 0), m), picked_by=ctx.user.id)
    ctx.db.add(part)
    ctx.db.flush()
    if not svc.reserve_from_stock(ctx.db, ctx.branch.id, part):
        ctx.db.rollback()
        flash(ctx.request, "Не получилось зарезервировать — остаток изменился, обновите страницу", "error")
        return _to(to)
    _after_part_added(ctx, wo, part)
    ctx.db.commit()
    flash(ctx.request, f"«{cat.name}» × {n.normalize():f} — в наряд {wo.number}, в резерве")
    return _to(to)


def _look(ctx: Ctx, q, sec, cat, sub, make, model, order, donor):
    """Склад глазами слесаря (11.11.2026): только свободное, без цен.
    ТО / Запчасти — фильтр по категории и по марке/модели машины.
    Разбор — сначала машина-донор (можно сузить маркой и моделью), потом её категории и детали.
    Купленное, в резерве под покупателя, ждущее отправки и проданное — не показываем (свободно = 0)."""
    from urllib.parse import urlencode
    from ..services import stock_cat as sc

    def link(**over):
        d = {"sec": sec, "q": q, "cat": cat, "sub": sub, "make": make, "model": model, "donor": donor, "order": order, **over}
        return "/stock?" + urlencode({k: v for k, v in d.items() if v})

    n_sec = {s: len(_stock_rows(ctx, "", "free", s, "", "", "", "")[0]) for s in SECTIONS}
    used_all = _stock_rows(ctx, "", "free", "used", "", "", "", "")[0]
    data = dict(q=q, sec=sec, cat=cat, sub=sub, make=make, model=model, order=order, donor=donor, link=link,
                n_sec=n_sec, n_used=len(used_all), fit_label=sc.fit_label, SECTIONS=SECTIONS)
    if sec != "used":
        rows, counts = _stock_rows(ctx, q, "free", sec, cat, sub, make, model)
        mm = sc.makes_models(ctx.db, ctx.branch.id)
        return ctx.render("stock/look.html", title="Склад", rows=rows, counts=counts, tree=sc.tree(ctx.db, ctx.branch.id, sec),
                          makes=mm, models=mm.get(make, []), **data)
    # ---- Разбор: машины-доноры со свободными деталями ----
    from ..models import Donor

    def key(r):
        i = r["i"]
        return str(i.donor_id) if i.donor_id else "mm:" + (i.donor_make or "Без машины") + "|" + (i.donor_model or "")
    cars: dict[str, dict] = {}
    donors = {d.id: d for d in ctx.db.scalars(select(Donor).where(Donor.branch_id == ctx.branch.id))}
    for r in used_all:
        i, k = r["i"], key(r)
        d = donors.get(i.donor_id)
        c = cars.setdefault(k, {"key": k, "make": (d.make if d else i.donor_make) or "Без машины",
                                "model": (d.model if d else i.donor_model) or "", "year": (d.year if d else i.donor_year),
                                "code": d.code if d else "", "n": 0})
        c["n"] += 1
    car_list = sorted(cars.values(), key=lambda c: (c["make"].lower(), c["model"].lower(), c["year"] or 0))
    makes_used = sorted({c["make"] for c in car_list}, key=str.lower)
    models_used = sorted({c["model"] for c in car_list if c["make"] == make and c["model"]}, key=str.lower)
    shown_cars = [c for c in car_list if (not make or c["make"] == make) and (not model or c["model"] == model)]
    rows, cats = [], {}
    if donor or q:
        ql = q.strip().lower()
        for r in used_all:
            if donor and key(r) != donor:
                continue
            if ql and ql not in f"{r['c'].name} {r['c'].article} {r['i'].code} {r['i'].donor_make} {r['i'].donor_model}".lower():
                continue
            k1, k2 = r["c"].category or "Прочее", r["c"].subcategory or ""
            cats.setdefault(k1, {"n": 0, "subs": {}})
            cats[k1]["n"] += 1
            if k2:
                cats[k1]["subs"][k2] = cats[k1]["subs"].get(k2, 0) + 1
            if cat and k1 != cat or (sub and k2 != sub):
                continue
            rows.append(r)
        rows.sort(key=lambda r: ((r["c"].category or "яя").lower(), (r["c"].subcategory or "").lower(), r["c"].name.lower()))
    return ctx.render("stock/look.html", title="Склад — разбор", rows=rows, cars=shown_cars, car=cars.get(donor),
                      ucats=sorted(cats.items(), key=lambda x: x[0].lower()), makes=makes_used, models=models_used, **data)


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
def stock_item(item_id: str, ctx: Ctx = Depends(get_ctx), order: str = ""):
    ctx.need("stock.view", "stock.look")
    from ..services import stock_cat as sc
    i = ctx.db.get(StockItem, _uuid(item_id))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    c = ctx.db.get(PartCatalog, i.catalog_id)
    look = not ctx.can("stock.view")   # слесарь: без движений, наклеек и цен
    moves = [] if look else ctx.db.scalars(select(StockMovement).where(StockMovement.stock_item_id == i.id)
                                           .order_by(StockMovement.created_at.desc()).limit(200)).all()
    ask_orders = []
    if ctx.can("parts.ask"):
        from ..services import messages as msg
        ask_orders = msg.my_orders(ctx)
    ask_vehicles = {o.id: ctx.db.get(Vehicle, o.vehicle_id) for o in ask_orders}
    orders = {o.id: o for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.id.in_([m.work_order_id for m in moves if m.work_order_id])))}
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.id.in_([m.created_by for m in moves if m.created_by])))}
    sc.seed(ctx.db, ctx.branch.id)
    ctx.db.commit()
    from ..services import barcode as bc
    return ctx.render("stock/item.html", title=c.name, i=i, c=c, b=svc.balances(ctx.db, ctx.branch.id, [i.id])[i.id],
                      loc=svc.location_label(ctx.db.get(Location, i.location_id) if i.location_id else None),
                      moves=moves, orders=orders, people=people, MOVEMENT_TYPES=MOVEMENT_TYPES,
                      look=look, ask_orders=ask_orders, ask_vehicles=ask_vehicles, sel_order=order,
                      free_all=svc.catalog_free(ctx.db, ctx.branch.id, [i.catalog_id])[i.catalog_id],
                      fmt_plate=osvc.fmt_plate,
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
    fact = _dec(actual, "-1")
    if fact < 0:
        flash(ctx.request, "Остаток по факту не может быть меньше нуля", "error")
        return _to(f"/stock/items/{item_id}")
    if fact < b["reserved"]:   # иначе «свободно» уходит в минус — резерв под наряды висит на пустом месте
        flash(ctx.request, f"На полке меньше, чем в резерве под наряды ({b['reserved'].normalize():f} шт). "
                           f"Сначала снимите резерв в нарядах (убрать позицию или отменить наряд)", "error")
        return _to(f"/stock/items/{item_id}")
    diff = fact - b["on_hand"]
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
                          "cells": cells, "status": part.status if part else "", "urgent": bool(part and part.urgent)})
        out.append({"r": r, "wo": wo, "v": db.get(Vehicle, wo.vehicle_id), "who": db.get(User, r.created_by),
                    "lines": lines})
    return out


@router.get("/stock/issues")
def issues(ctx: Ctx = Depends(get_ctx), tab: str = "parts"):
    """Выдача: вкладки «Запчасти» и «Инструмент» (11.11.2026) — что выдать, что сдают, что на руках."""
    ctx.need("stock.issue", "stock.view", "tools.manage")
    from ..services import tools as tsvc
    can_tools = ctx.can("tools.manage", "tools.view")
    tool_new = tsvc.requests_list(ctx.db, ctx.branch.id, "new") if can_tools else []
    new = _requests(ctx, "new")
    # 12.11.2026: химия для мойки — мойщик / детейлер просит, кладовщик выдаёт (расход СТО)
    from ..services import chem as csvc
    can_chem = ctx.can("stock.issue")
    chem_new = csvc.requests(ctx.db, ctx.branch.id, "new", days=0) if can_chem else []
    if tab not in ("parts", "tools", "chem") or (tab == "tools" and not can_tools) or (tab == "chem" and not can_chem):
        tab = "parts"
    data = dict(tab=tab, new=new, n_parts=len(new), can_tools=can_tools, n_tools=len(tool_new),
                can_chem=can_chem, n_chem=len(chem_new), fmt_plate=osvc.fmt_plate)
    if tab == "chem":
        free = {x["c"].id: x for x in csvc.items(ctx.db, ctx.branch.id)}
        data.update(chem_new=chem_new, chem_free=free, chem_done=csvc.requests(ctx.db, ctx.branch.id, ("issued", "declined"), days=7)[:20],
                    locs={l.id: svc.location_label(l) for l in ctx.db.scalars(select(Location).where(Location.branch_id == ctx.branch.id))})
    elif tab == "tools":
        data.update(t_issue=[x for x in tool_new if x["r"].kind == "issue"], t_return=[x for x in tool_new if x["r"].kind == "return"],
                    t_out=tsvc.out_on_orders(ctx.db, ctx.branch.id), t_done=tsvc.requests_list(ctx.db, ctx.branch.id, "done", 15),
                    can_manage=ctx.can("tools.manage"))
    else:
        # 11.11.2026: слесарь попросил запчасть — кладовщик видит заранее, но выдаёт только после согласования менеджера
        from ..services import messages as msg
        pend = []
        for m in msg.pending_parts(ctx.db, ctx.branch.id):
            wo = ctx.db.get(WorkOrder, m.work_order_id)
            item = ctx.db.get(StockItem, m.stock_item_id) if m.stock_item_id else None
            pend.append({"m": m, "wo": wo, "v": ctx.db.get(Vehicle, wo.vehicle_id), "cat": ctx.db.get(PartCatalog, m.catalog_id),
                         "who": ctx.db.get(User, m.created_by), "mgr": ctx.db.get(User, wo.manager_id) if wo.manager_id else None,
                         "cell": svc.location_label(ctx.db.get(Location, item.location_id) if item and item.location_id else None)})
        data["pend"] = pend
        data["done"] = _requests(ctx, "issued")[:15]
        data["ready"] = _requests(ctx, "ready")
        data["people"] = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("stock/issues.html", title="Запросы на выдачу", **data)


@router.post("/stock/issues/{rid}/issue")
def issue_do(rid: str, ctx: Ctx = Depends(get_ctx), back: str = Form("")):
    """«Выдано»: кладовщик — сразу или после «Готово к выдаче»; менеджер — только подготовленное (12.11.2026)."""
    ctx.need("stock.issue", "stock.pickup")
    r = ctx.db.get(IssueRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id:
        raise Forbidden()
    ok = ("new", "ready") if ctx.can("stock.issue") else ("ready",)
    if r.status in ok:
        svc.issue(ctx.db, r, ctx.user.id)
        ctx.db.commit()
        flash(ctx.request, "Выдано")
    elif r.status == "new":
        raise Forbidden()
    return _to(_safe_back(back, "/stock/issues" if ctx.can("stock.issue") else "/ready"))


@router.post("/stock/issues/{rid}/ready")
def issue_ready(rid: str, ctx: Ctx = Depends(get_ctx)):
    """Кладовщик собрал запчасти у окошка — «Готово к выдаче». У менеджера загорается."""
    ctx.need("stock.issue")
    r = ctx.db.get(IssueRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id:
        raise Forbidden()
    if svc.mark_ready(ctx.db, r, ctx.user):
        ctx.db.commit()
        flash(ctx.request, "Готово к выдаче — менеджер получил сообщение")
    return _to("/stock/issues")


def _ready_mine(ctx: Ctx, x) -> bool:
    return x["wo"].manager_id == ctx.user.id or x["r"].created_by == ctx.user.id


@router.get("/ready")
def ready_page(ctx: Ctx = Depends(get_ctx)):
    """Запросы на выдачу (менеджер; до 12.11.2026 — «Готово к выдаче»): по моим нарядам — что ждёт кладовщика,
    что он подготовил (забрать → «Выдано»), что недавно выдано. Подготовленное по чужим нарядам — ниже."""
    ctx.need("stock.pickup", "stock.issue")
    rows = _requests(ctx, "ready")
    rows.sort(key=lambda x: x["r"].ready_at or x["r"].created_at)
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("stock/ready.html", title="Запросы на выдачу", mine=[x for x in rows if _ready_mine(ctx, x)],
                      others=[x for x in rows if not _ready_mine(ctx, x)], people=people, fmt_plate=osvc.fmt_plate,
                      waiting=[x for x in _requests(ctx, "new") if _ready_mine(ctx, x)],
                      done=[x for x in _requests(ctx, "issued")[:30] if _ready_mine(ctx, x)][:10])


def my_requests_data(ctx: Ctx) -> dict:
    """«Мои запросы на выдачу» мастера (12.11.2026): запчасти по нарядам, где он мастер (просил он или менеджер),
    его просьбы «под наряд» со склада, инструмент, химия. Открытые — все, закрытые — за 14 дней."""
    from datetime import timedelta
    from ..db import now
    from ..models import ToolRequest
    from ..services import notify as nsvc
    db, me, since = ctx.db, ctx.user.id, now() - timedelta(days=14)
    out = {"parts": [], "asks": [], "tools": [], "chem": []}
    if ctx.can("stock.request_issue"):
        for st in ("ready", "new", "issued"):
            for x in _requests(ctx, st):
                if st == "issued" and (x["r"].issued_at or x["r"].updated_at) < since:
                    continue
                if x["r"].created_by == me or me in nsvc.masters(db, x["wo"]):
                    out["parts"].append(x)
        from ..models import OrderMessage
        for m in db.scalars(select(OrderMessage).where(OrderMessage.branch_id == ctx.branch.id, OrderMessage.kind == "part",
                                                       OrderMessage.created_by == me, OrderMessage.created_at >= since)
                            .order_by(OrderMessage.created_at.desc())):
            wo = db.get(WorkOrder, m.work_order_id)
            out["asks"].append({"m": m, "wo": wo, "v": db.get(Vehicle, wo.vehicle_id) if wo else None,
                                "c": db.get(PartCatalog, m.catalog_id) if m.catalog_id else None})
        from ..models import Tool
        for r in db.scalars(select(ToolRequest).where(ToolRequest.branch_id == ctx.branch.id, ToolRequest.user_id == me)
                            .order_by(ToolRequest.created_at.desc()).limit(60)):
            if r.status != "new" and (r.done_at or r.updated_at) < since:
                continue
            wo = db.get(WorkOrder, r.work_order_id) if r.work_order_id else None
            out["tools"].append({"r": r, "tool": db.get(Tool, r.tool_id), "wo": wo})
    if ctx.can("chem.ask"):
        from ..services import chem as csvc
        out["chem"] = csvc.requests(db, ctx.branch.id, who=me, days=14)
    return out


@router.get("/my-requests")
def my_requests(ctx: Ctx = Depends(get_ctx), tab: str = ""):
    ctx.need("stock.request_issue", "chem.ask")
    from ..services import chem as csvc
    data = my_requests_data(ctx)
    tabs = ([("parts", "Запчасти"), ("tools", "Инструмент")] if ctx.can("stock.request_issue") else []) \
        + ([("chem", "Химия")] if ctx.can("chem.ask") else [])
    tab = tab if tab in dict(tabs) else tabs[0][0]
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("stock/my_requests.html", title="Мои запросы на выдачу", tab=tab, tabs=tabs, people=people,
                      fmt_plate=osvc.fmt_plate, CHEM_STATUS=csvc.STATUS, **data)


@router.get("/api/notify")
def notify(ctx: Ctx = Depends(get_ctx)):
    """Опрос раз в 15 секунд. Кладовщику — новые запросы на выдачу; всем — их сообщения
    («Все запчасти на складе», «Пришли запчасти», «Срочная деталь пришла», «Слесарь просит срочно»)."""
    from ..services import badges as bsvc
    from ..services import notify as nsvc
    notes = []
    for n in nsvc.unread(ctx.db, ctx.user.id):
        wo = ctx.db.get(WorkOrder, n.work_order_id) if n.work_order_id else None
        can_req = bool(wo and wo.status in ("instock", "work") and ctx.can("stock.request_issue")
                       and ctx.user.id in nsvc.masters(ctx.db, wo)   # любой мастер наряда (12.11.2026)
                       and not ctx.db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == wo.id,
                                                                            IssueRequest.status == "new"))
                       and ctx.db.scalar(select(WoPart.id).where(WoPart.work_order_id == wo.id, WoPart.status == "reserved")))
        notes.append({"id": str(n.id), "kind": n.kind, "title": n.title, "text": n.text, "url": n.url,
                      "request_url": f"/orders/{wo.id}/issue-request" if can_req else ""})
    unread_n = len(notes)
    razbor_n = 0
    if ctx.can("razbor.manage"):   # «Выдача и отправка» разбора: сколько резервов ещё не подготовлено
        from ..models import UsedSale
        razbor_n = ctx.db.scalar(select(func.count()).select_from(UsedSale).where(
            UsedSale.branch_id == ctx.branch.id, UsedSale.status == "reserved", UsedSale.packed_at.is_(None))) or 0
    # Инструмент (11.11.2026): кладовщику — «просит выдать» / «сдаёт», как запросы на запчасти
    tools_items = []
    if ctx.can("tools.manage"):
        from ..services import tools as tsvc
        for x in tsvc.requests_list(ctx.db, ctx.branch.id, "new", 10):
            r, t = x["r"], x["tool"]
            tools_items.append({"id": "t" + str(r.id), "kind": r.kind, "who": x["who"].full_name if x["who"] else "",
                                "text": (t.name if t else "") + (f" · {t.place}" if t and t.place else ""),
                                "order": x["wo"].number if x["wo"] else "", "plate": osvc.fmt_plate(x["v"].plate) if x["v"] else "",
                                "done_url": f"/tool-requests/{r.id}/done"})
    if not ctx.can("stock.issue"):
        ready_n = 0
        if ctx.can("stock.pickup"):   # «Готово к выдаче» — по моим нарядам и моим запросам
            ready_n = sum(1 for x in _requests(ctx, "ready") if _ready_mine(ctx, x))
        return {"issues": len(tools_items), "items": [], "tools": tools_items, "notes": notes, "unread": unread_n,
                "ready": ready_n, "razbor": razbor_n, "badges": bsvc.compute(ctx)}
    items = []
    for x in _requests(ctx, "new")[:5]:
        names = ", ".join(("🔥 " if l["urgent"] else "") + l["name"] for l in x["lines"][:3]) + ("…" if len(x["lines"]) > 3 else "")
        items.append({"id": str(x["r"].id), "who": x["who"].full_name if x["who"] else "",
                      "text": names, "order": x["wo"].number, "plate": osvc.fmt_plate(x["v"].plate)})
    return {"issues": len(_requests(ctx, "new")) + len(tools_items), "items": items, "tools": tools_items,
            "notes": notes, "unread": unread_n, "razbor": razbor_n, "badges": bsvc.compute(ctx),
            "receive": _receive_count(ctx) if ctx.can("stock.manage") else 0}


@router.post("/api/notify/{nid}/read")
def notify_read(nid: str, ctx: Ctx = Depends(get_ctx)):
    from ..services import notify as nsvc
    if ctx.viewing_as:   # админ смотрит глазами сотрудника — его сообщения не трогаем, пусть увидит сам
        return {"ok": True}
    nsvc.mark_read(ctx.db, ctx.user.id, None if nid == "all" else _uuid(nid))
    ctx.db.commit()
    return {"ok": True}


@router.get("/notifications")
def notifications(ctx: Ctx = Depends(get_ctx), order: str = ""):
    """«Сообщения» (11.11.2026): переписка по нарядам (слесарь ↔ менеджер ↔ запчастист), запросы запчастей
    на согласование и уведомления («Все запчасти на складе», «Вам передан наряд»…)."""
    from ..models import OrderMessage, PartCatalog
    from ..services import messages as msg
    from ..services import notify as nsvc
    rows = nsvc.recent(ctx.db, ctx.user.id)
    if not ctx.viewing_as:   # глазами сотрудника — не отмечаем его сообщения прочитанными
        nsvc.mark_read(ctx.db, ctx.user.id)
        ctx.db.commit()
    mine = msg.visible(ctx)
    # Менеджеру — все ждущие согласования по его нарядам (даже если писали не лично ему)
    pend = [m for m in msg.pending_parts(ctx.db, ctx.branch.id)
            if msg.can_decide(ctx, ctx.db.get(WorkOrder, m.work_order_id))]
    ids = {m.id for m in mine}
    mine = [m for m in pend if m.id not in ids] + mine
    orders = {o.id: o for o in ctx.db.scalars(select(WorkOrder).where(WorkOrder.id.in_([m.work_order_id for m in mine])))}
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    cats = {c.id: c for c in ctx.db.scalars(select(PartCatalog).where(PartCatalog.id.in_([m.catalog_id for m in mine if m.catalog_id])))}
    prices = {m.id: msg.client_price(ctx.db, ctx.branch.id, ctx.db.get(StockItem, m.stock_item_id))
              for m in pend if m.stock_item_id}
    write_orders = msg.my_orders(ctx, msg.OPEN_STATUSES)
    import uuid as _u

    def _names(m):
        out = []
        for x in m.to_ids or []:
            try:
                u = people.get(_u.UUID(str(x)))
            except ValueError:
                u = None
            if u:
                out.append(u.short_name)
        return ", ".join(out)
    return ctx.render("notifications.html", title="Сообщения", rows=rows, msgs=mine, pend_ids={m.id for m in pend},
                      to_names={m.id: _names(m) for m in mine},
                      orders=orders, people=people, cats=cats, prices=prices, write_orders=write_orders,
                      teams={o.id: {k: (u if u and u.id != ctx.user.id else None) for k, u in msg.team(ctx.db, o).items()}
                             for o in write_orders}, sel_order=order,
                      vehicles={o.id: ctx.db.get(Vehicle, o.vehicle_id) for o in [*write_orders, *orders.values()]},
                      fmt_plate=osvc.fmt_plate, show_price=ctx.can("prices.view_sale"))


def _msg_order(ctx: Ctx, order_id: str) -> WorkOrder:
    """Наряд, в команде которого я есть (или который вижу) — писать можно только по таким."""
    from ..services import messages as msg
    wo = ctx.db.get(WorkOrder, _uuid(order_id))
    if not wo or wo.branch_id != ctx.branch.id:
        raise Forbidden()
    if wo.id not in {o.id for o in msg.my_orders(ctx, msg.OPEN_STATUSES)} and not osvc.can_view(ctx, wo):
        raise Forbidden()
    return wo


@router.post("/messages")
def message_send(ctx: Ctx = Depends(get_ctx), order_id: str = Form(""), to: list[str] = Form([]), text: str = Form(""),
                 back: str = Form("")):
    """Текстовое сообщение команде наряда: менеджеру, запчастисту, слесарю."""
    from ..services import messages as msg
    back = _safe_back(back, "/notifications")
    if not order_id:
        flash(ctx.request, "Выберите машину (наряд)", "error")
        return _to(back)
    wo = _msg_order(ctx, order_id)
    t = msg.team(ctx.db, wo)
    err = msg.send_text(ctx.db, ctx, wo, [t[k].id for k in to if k in t and t[k]], text)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Сообщение отправлено")
    return _to(back)


@router.post("/messages/ask")
def message_ask(ctx: Ctx = Depends(get_ctx), item_id: str = Form(...), order_id: str = Form(""), qty: str = Form("1"),
                text: str = Form("")):
    """Слесарь просит запчасть со склада под свой наряд — согласует менеджер."""
    from ..services import messages as msg
    ctx.need("parts.ask")
    item = ctx.db.get(StockItem, _uuid(item_id))
    if not item or item.branch_id != ctx.branch.id:
        raise Forbidden()
    back = f"/stock/items/{item.id}"
    mine = {str(o.id): o for o in msg.my_orders(ctx)}
    wo = mine.get(order_id)
    if not wo:
        flash(ctx.request, "Выберите свою машину (наряд в ремонте)", "error")
        return _to(back)
    err = msg.ask_part(ctx.db, ctx, wo, item, _dec(qty, "0"), text)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
        return _to(back)
    ctx.db.commit()
    flash(ctx.request, "Запрос отправлен менеджеру. Кладовщик выдаст после согласования")
    return _to(back)


@router.post("/messages/{mid}/decide")
def message_decide(mid: str, ctx: Ctx = Depends(get_ctx), action: str = Form(""), price: str = Form(""),
                   answer: str = Form("")):
    """Менеджер: «Согласовать» (запчасть в наряд, кладовщику на выдачу) или «Отказать» (с причиной)."""
    from ..models import OrderMessage
    from ..services import messages as msg
    m = ctx.db.get(OrderMessage, _uuid(mid))
    if not m or m.branch_id != ctx.branch.id or m.kind != "part":
        raise Forbidden()
    wo = ctx.db.get(WorkOrder, m.work_order_id)
    if not msg.can_decide(ctx, wo):
        raise Forbidden()
    if action == "approve":
        pr = None
        if price.strip() and ctx.can("prices.view_sale"):
            pr = _dec(price, "-1")
            if pr < 0:
                flash(ctx.request, "Цена не может быть меньше нуля", "error")
                return _to(f"/notifications#m-{m.id}")
        err = msg.approve(ctx.db, ctx, m, pr, answer)
    else:
        err = msg.decline(ctx.db, ctx, m, answer)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Согласовано — кладовщик получил запрос на выдачу" if action == "approve" else "Отказано — слесарь получил ответ")
    return _to(f"/notifications#m-{m.id}")


# ======================= Срочная деталь (11.11.2026) =======================
@router.post("/orders/{order_id}/parts/{part_id}/urgent")
def part_urgent(order_id: str, part_id: str, ctx: Ctx = Depends(get_ctx)):
    """Слесарь (или менеджер) отмечает: деталь нужна срочно. Видят все по цепочке; запчастисту и менеджеру — сообщение."""
    ctx.need("orders.view_own", "orders.view_all")
    wo = _order(ctx, order_id)
    part = ctx.db.get(WoPart, _uuid(part_id))
    allowed = ctx.user.id == wo.mechanic_id or ctx.can("orders.approve") or ctx.user.role.code == "admin"
    if not allowed or not part or part.work_order_id != wo.id or part.status in ("issued", "installed", "declined", "returned"):
        raise Forbidden()
    part.urgent = not part.urgent
    if part.urgent:
        from ..services import notify as nsvc
        nsvc.send(ctx.db, [wo.partsman_id, wo.manager_id, wo.mechanic_id], "urgent_ask",
                  f"Срочно нужна: {part.name}", f"{ctx.user.short_name} · {nsvc.car(ctx.db, wo)} · наряд {wo.number}",
                  f"/orders/{wo.id}#parts", wo, skip=ctx.user.id)
    ctx.db.commit()
    if ctx.request.headers.get("x-requested-with") == "fetch":
        return {"ok": True, "urgent": bool(part.urgent)}
    flash(ctx.request, f"«{part.name}» — {'срочно' if part.urgent else 'пометка «срочно» снята'}")
    return _to(f"/orders/{order_id}#parts")


# ======================= Приход у кладовщика (11.11.2026) =======================
def _sup_key(sup) -> str:
    """Группа «Прихода» — по названию поставщика (одинаковые названия — один поставщик, даже если заведён дважды)."""
    return sup.name.strip().lower() if sup else "none"


def _receive_lines(ctx: Ctx, who: str = ""):
    """Что ждём от поставщиков: непринятые строки заказанных закупок, по закупкам; срочное — сверху.
    who — только закупки этого запчастиста (12.11.2026)."""
    db = ctx.db
    purchases = db.scalars(select(Purchase).where(Purchase.branch_id == ctx.branch.id,
                                                  Purchase.status.in_(svc.OPEN)).order_by(Purchase.created_at)).all()
    if who:
        purchases = [p for p in purchases if str(p.created_by) == who]
    # 13.11.2026 (брат): по ПОСТАВЩИКАМ — приехал поставщик, кладовщик видит всё, что ждём от него, по всем нарядам
    by_sup: dict = {}
    for p in purchases:
        buyer = db.get(User, p.created_by) if p.created_by else None
        sup = db.get(Supplier, p.supplier_id) if p.supplier_id else None
        key = _sup_key(sup)
        for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id, PurchaseLine.received.is_(False))):
            part = db.get(WoPart, l.wo_part_id) if l.wo_part_id else None
            wo = db.get(WorkOrder, part.work_order_id) if part else None
            g = by_sup.setdefault(key, {"sup": sup, "key": key, "rows": [], "ps": [], "buyers": []})
            if p not in g["ps"]:
                g["ps"].append(p)
            if buyer and buyer.short_name not in g["buyers"]:
                g["buyers"].append(buyer.short_name)
            g["rows"].append({"l": l, "p": p, "c": db.get(PartCatalog, l.catalog_id), "part": part, "wo": wo, "buyer": buyer,
                              "v": db.get(Vehicle, wo.vehicle_id) if wo else None,
                              "mech": db.get(User, wo.mechanic_id) if wo and wo.mechanic_id else None,
                              "urgent": bool(part and part.urgent), "cell": svc.default_location(db, l.catalog_id)})
    groups = list(by_sup.values())
    for g in groups:
        g["rows"].sort(key=lambda r: (not r["urgent"], r["wo"] is None, r["wo"].number if r["wo"] else "",
                                      r["c"].name if r["c"] else ""))
        g["urgent"] = any(r["urgent"] for r in g["rows"])
        g["invoices"] = [p.supplier_invoice for p in g["ps"] if p.supplier_invoice]
    groups.sort(key=lambda g: (not g["urgent"], g["sup"].name if g["sup"] else "я"))
    return groups


def _receive_count(ctx: Ctx) -> int:
    return ctx.db.scalar(select(func.count()).select_from(PurchaseLine).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                         .where(Purchase.branch_id == ctx.branch.id, Purchase.status.in_(svc.OPEN),
                                PurchaseLine.received.is_(False))) or 0


@router.get("/stock/receive")
def receive_page(ctx: Ctx = Depends(get_ctx), who: str = ""):
    ctx.need("stock.manage")
    db = ctx.db
    locations = db.scalars(select(Location).where(Location.branch_id == ctx.branch.id)
                           .order_by(Location.rack, Location.shelf, Location.place)).all()
    last = ctx.request.session.pop("last_received", None)
    wu = db.get(User, _uuid(who)) if who else None
    drafts = svc.draft_waits(db, ctx.branch.id, wu.id if wu else None)
    return ctx.render("stock/receive.html", title="Приход", groups=_receive_lines(ctx, who), locations=locations, who_user=wu,
                      last=last, loc_label=svc.location_label, fmt_plate=osvc.fmt_plate, drafts=drafts,
                      dveh={d["wo"].id: db.get(Vehicle, d["wo"].vehicle_id) for d in drafts})


@router.post("/stock/receive")
async def receive_do(ctx: Ctx = Depends(get_ctx)):
    """«Пришло» по строке или «Принять всё» по закупке. Ячейка — выбранная или та, где запчасть уже лежит."""
    ctx.need("stock.manage")
    form = await ctx.request.form()
    purchases = ctx.db.scalars(select(Purchase).where(Purchase.branch_id == ctx.branch.id,
                                                      Purchase.status.in_(svc.OPEN))).all()
    done, ready = _receive(ctx, form, purchases)
    np_, ni_ = ctx.request.state.no_price, ctx.request.state.no_inv
    flash(ctx.request, f"Принято: {done}" + (f". Все запчасти на складе: {', '.join(ready)} — менеджер и слесарь получили сообщение" if ready else "")
          + (f". Без цены не принято: {', '.join(np_[:5])} — впишите «₴ за шт» из накладной" if np_ else "")
          + (f". Без № накладной не принято: {', '.join(ni_[:5])} — впишите номер" if ni_ else ""),
          "error" if np_ or ni_ else "ok")
    who = str(form.get("who", ""))
    return _to("/stock/receive" + (f"?who={who}" if who else ""))


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
