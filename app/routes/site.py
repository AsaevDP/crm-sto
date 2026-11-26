"""«На сайт» (22.11.2026, брат): две вкладки — «Новые» (новые запчасти на продажу) и «Б/у · разбор».
Видят: кладовщик разбора и главный кладовщик (razbor.export), директор и зам (razbor.price), админ.
Подробно — services/site.py."""
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import PartCatalog, StockItem
from ..models.money import PAY_METHODS
from ..models.razbor import CHANNELS, SiteNew, SiteNewPhoto
from ..services import orders as osvc
from ..services import razbor as rsvc
from ..services import site as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()


def _to(url):
    return RedirectResponse(url, status_code=303)


def _sn(ctx: Ctx, sid: str) -> SiteNew:
    s = ctx.db.get(SiteNew, _uuid(sid))
    if not s or s.branch_id != ctx.branch.id:
        raise Forbidden()
    return s


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", ""))
    except Exception:
        return Decimal(default)


@router.get("/site")
def site_page(ctx: Ctx = Depends(get_ctx), t: str = "", f: str = ""):
    ctx.need("razbor.export", "razbor.price")
    db, bid = ctx.db, ctx.branch.id
    svc.sync_leftovers(db, bid)
    db.commit()
    new_rows = svc.rows(db, bid)
    counts = {k: sum(1 for r in new_rows if r["stage"] == k) for k in svc.STAGES}
    # Б/у · разбор (26.11.2026): «Выложить на сайт» — утверждено, в наличии, «Выложил» не нажимали; «Выложены на сайт»
    from ..models import StockPhoto
    from sqlalchemy import func
    from .razbor import _item_rows
    used = db.scalars(select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == bid, PartCatalog.kind == "used").order_by(StockItem.code.desc())).all()
    all_used = [r for r in _item_rows(ctx, used) if r["state"] == "stock"]
    nph = dict(db.execute(select(StockPhoto.stock_item_id, func.count()).where(
        StockPhoto.stock_item_id.in_([r["i"].id for r in all_used])).group_by(StockPhoto.stock_item_id)).all()) if all_used else {}
    for r in all_used:
        r["missing"] = svc.post_missing(nph.get(r["i"].id, 0), r["i"].description)
        r["n_photos"] = nph.get(r["i"].id, 0)
    used_post = [r for r in all_used if not r["i"].publish_on_site]
    used_site = [r for r in all_used if r["i"].publish_on_site]
    if t not in ("new", "used"):   # открываем вкладку, где есть дело
        t = "used" if used_post and not counts["post"] and ctx.can("razbor.export") else "new"
    if t == "new" and f not in svc.STAGES:
        f = next((k for k in (("wait", "post", "declined") if ctx.can("razbor.price") else ("post", "declined", "wait"))
                  if counts[k]), "site")
    if t == "used" and f not in ("post", "site"):
        f = "post" if used_post else "site"
    shown = [r for r in new_rows if r["stage"] == f] if t == "new" else []
    used_rows = (used_post if f == "post" else used_site) if t == "used" else []
    return ctx.render("site/index.html", title="На сайт", t=t, f=f, rows=shown, counts=counts, STAGES=svc.STAGES,
                      used_rows=used_rows, n_used_post=len(used_post), n_used_site=len(used_site), MIN_PHOTOS=svc.MIN_PHOTOS,
                      n_new_site=counts["site"], n_used=len(used_site),
                      can_price=ctx.can("razbor.price"), can_edit=ctx.can("razbor.export"),
                      can_sell=ctx.can("razbor.sell"), can_export=ctx.can("razbor.export"),
                      CHANNELS=CHANNELS, PAY_METHODS=PAY_METHODS, fmt_phone=osvc.fmt_phone)


@router.post("/site/new/{sid}/approve")
def approve(sid: str, ctx: Ctx = Depends(get_ctx), price: str = Form("")):
    ctx.need("razbor.price")
    s = _sn(ctx, sid)
    err = svc.approve(ctx.db, s, ctx.user, _dec(price) if price.strip() else None)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"{s.code}: цена {s.price:.2f} ₴ утверждена — кладовщик разбора выложит деталь на сайт")
    return _to("/site?t=new&f=wait")


@router.post("/site/new/{sid}/resubmit")
def resubmit(sid: str, ctx: Ctx = Depends(get_ctx), note: str = Form("")):
    """26.11.2026: отказано → «Отправить ещё раз» с комментарием (кладовщик разбора)."""
    ctx.need("razbor.export")
    s = _sn(ctx, sid)
    err = svc.resubmit(ctx.db, s, note)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"{s.code}: снова отправлено директору на утверждение")
    return _to("/site?t=new&f=declined")


@router.post("/site/new/{sid}/post")
def post(sid: str, ctx: Ctx = Depends(get_ctx)):
    """26.11.2026: «✓ Выложил на сайт» — фото ≥ 2 и описание."""
    ctx.need("razbor.export")
    s = _sn(ctx, sid)
    err = svc.post_new(ctx.db, s, ctx.user)
    if err:
        flash(ctx.request, err, "error")
        return _to(f"/site?t=new&f=post#sn{s.id}")
    ctx.db.commit()
    flash(ctx.request, f"{s.code}: выложена на сайт")
    return _to("/site?t=new&f=post")


@router.post("/site/new/{sid}/unpost")
def unpost(sid: str, ctx: Ctx = Depends(get_ctx)):
    """26.11.2026: «Снять с сайта» — снова «Выложить на сайт»."""
    ctx.need("razbor.export")
    s = _sn(ctx, sid)
    s.posted_at, s.posted_by = None, None
    ctx.db.commit()
    flash(ctx.request, f"{s.code}: снята с сайта, снова в «Выложить на сайт»")
    return _to("/site?t=new&f=site")


@router.post("/site/new/{sid}/decline")
def decline(sid: str, ctx: Ctx = Depends(get_ctx), answer: str = Form("")):
    ctx.need("razbor.price")
    s = _sn(ctx, sid)
    err = svc.decline(ctx.db, s, ctx.user, answer)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"{s.code}: отказано в утверждении")
    return _to("/site?t=new&f=wait")


@router.post("/site/new/{sid}")
async def edit(sid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Кладовщик разбора: описание для сайта, фото, «показывать на сайте», снять с продажи / вернуть. Цену не меняет."""
    ctx.need("razbor.export")
    s = _sn(ctx, sid)
    form = await request.form()
    action = str(form.get("action", "save"))
    if action == "off":
        s.status = "off"
        flash(ctx.request, f"{s.code}: снята с продажи")
    elif action == "on":
        s.status = "on"
        flash(ctx.request, f"{s.code}: снова на продаже")
    elif action == "photo_del":
        p = ctx.db.get(SiteNewPhoto, _uuid(str(form.get("photo", ""))))
        if p and p.site_new_id == s.id:
            ctx.db.delete(p)
            ctx.db.flush()
            from sqlalchemy import func
            n = ctx.db.scalar(select(func.count()).select_from(SiteNewPhoto).where(SiteNewPhoto.site_new_id == s.id)) or 0
            if s.posted_at and n < svc.MIN_PHOTOS:   # 26.11.2026: фото меньше нормы — снова «Выложить на сайт»
                s.posted_at, s.posted_by = None, None
                flash(ctx.request, f"Фото удалено — фото меньше {svc.MIN_PHOTOS}, деталь снова в «Выложить на сайт»")
            else:
                flash(ctx.request, "Фото удалено")
    else:
        s.description = str(form.get("description", "")).strip()[:2000]
        if s.posted_at and not s.description:   # 26.11.2026: без описания на сайте не держим
            s.posted_at, s.posted_by = None, None
        n = 0
        for up in form.getlist("photos"):
            if hasattr(up, "read"):
                data = await up.read()
                if data:
                    path = rsvc.save_photo(data)
                    if path:
                        ctx.db.add(SiteNewPhoto(site_new_id=s.id, file=path))
                        n += 1
        flash(ctx.request, f"{s.code}: сохранено" + (f", фото: {n}" if n else ""))
    ctx.db.commit()
    return _to(str(form.get("back", "")) if str(form.get("back", "")).startswith("/site") else "/site?t=new")


@router.post("/site/new/{sid}/sell")
async def sell(sid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Продать новую деталь: резерв под покупателя или сразу «продано» — дальше как у б/у («Выдача и отправка»)."""
    ctx.need("razbor.sell")
    from ..models.money import PAY_METHODS
    from ..models.razbor import DELIVERY
    s = _sn(ctx, sid)
    form = await request.form()
    action = "sold" if form.get("action") == "sold" else "reserve"
    res = svc.sell(ctx.db, ctx.branch.id, s, _dec(form.get("qty"), "1"), action,
                   buyer_name=str(form.get("buyer_name", "")), buyer_phone=str(form.get("buyer_phone", "")),
                   delivery=str(form.get("delivery", "pickup")) if form.get("delivery") in DELIVERY else "pickup",
                   pay_method=str(form.get("pay_method", "cash")) if form.get("pay_method") in PAY_METHODS else "cash",
                   comment=str(form.get("comment", "")), np_address=str(form.get("np_address", "")),
                   cod=form.get("cod") == "1", channel=str(form.get("channel", "")) if form.get("channel") in CHANNELS else "",
                   ttn=str(form.get("ttn", "")))
    if isinstance(res, str):
        flash(ctx.request, res, "error")
        return _to("/site?t=new&f=site")
    ctx.db.commit()
    if action == "sold":
        flash(ctx.request, f"{s.code} — продано")
    else:
        flash(ctx.request, f"{s.code} в резерве под покупателя — дальше в «Выдача и отправка»")
    return _to("/site?t=new&f=site")


# ======================= «Заявки с сайта» (22.11.2026, брат) =======================
ORDER_TABS = {"new": ("Новые", "red", "принять или отказать"), "accepted": ("Приняты", "green", "детали в резерве"),
              "declined": ("Отказ", "gray", "с причиной")}


def _so(ctx: Ctx, oid: str):
    from ..models import SiteOrder
    o = ctx.db.get(SiteOrder, _uuid(oid))
    if not o or o.branch_id != ctx.branch.id:
        raise Forbidden()
    return o


@router.get("/site/orders")
def orders_page(ctx: Ctx = Depends(get_ctx), tab: str = ""):
    ctx.need("site.orders")
    from ..models import SiteOrder, User
    from ..models.razbor import ORDER_DELIVERY, ORDER_PAY
    from ..services import settings as st_
    from ..services import site_orders as sosvc
    db, bid = ctx.db, ctx.branch.id
    allo = db.scalars(select(SiteOrder).where(SiteOrder.branch_id == bid).order_by(SiteOrder.number.desc()).limit(300)).all()
    counts = {k: sum(1 for o in allo if o.status == k) for k in ORDER_TABS}
    if tab not in ORDER_TABS:
        tab = "new" if counts["new"] or not allo else "accepted"
    shown = [o for o in allo if o.status == tab]
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == bid))}
    api_key = st_.get(db, bid, "site_api").get("key", "") if ctx.user.role.code == "admin" else ""
    return ctx.render("site/orders.html", title="Заявки с сайта", tab=tab, TABS=ORDER_TABS, counts=counts,
                      orders=[{"o": o, "lines": sosvc.lines_of(db, bid, o)} for o in shown], people=people,
                      DELIVERY=ORDER_DELIVERY, PAY=ORDER_PAY, fmt_phone=osvc.fmt_phone, api_key=api_key,
                      is_admin=ctx.user.role.code == "admin")


def _order_form(form) -> tuple[dict, list[dict]]:
    data = {k: str(form.get(k, "")) for k in ("last_name", "first_name", "phone", "delivery", "city", "np_branch",
                                              "address", "pay", "comment")}
    codes, qtys, prices = form.getlist("code"), form.getlist("qty"), form.getlist("price")
    lines = [{"code": str(c), "qty": str(qtys[i]) if i < len(qtys) else "1", "price": str(prices[i]) if i < len(prices) else ""}
             for i, c in enumerate(codes) if str(c).strip()]
    return data, lines


@router.post("/site/orders")
async def orders_add(request: Request, ctx: Ctx = Depends(get_ctx)):
    """«＋ Заявка с сайта» — вписать руками (пока сайт не присылает сам: письмо, звонок с сайта)."""
    ctx.need("site.orders")
    from ..services import site_orders as sosvc
    data, lines = _order_form(await request.form())
    res = sosvc.create(ctx.db, ctx.branch.id, data, lines, "manual")
    if isinstance(res, list):
        flash(ctx.request, "Заполните: " + ", ".join(res), "error")
        return _to("/site/orders?tab=new")
    ctx.db.commit()
    flash(ctx.request, f"Заявка {res.code} добавлена — проверьте детали и примите")
    return _to("/site/orders?tab=new")


@router.post("/site/orders/{oid}/accept")
def orders_accept(oid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("site.orders")
    from ..services import site_orders as sosvc
    o = _so(ctx, oid)
    err = sosvc.accept(ctx.db, ctx.branch.id, o, ctx.user.id)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
        return _to("/site/orders?tab=new")
    ctx.db.commit()
    flash(ctx.request, f"Заявка {o.code} принята — детали в резерве, дальше «Выдача и отправка»")
    return _to("/site/orders?tab=accepted")


@router.post("/site/orders/{oid}/decline")
def orders_decline(oid: str, ctx: Ctx = Depends(get_ctx), reason: str = Form("")):
    ctx.need("site.orders")
    from ..services import site_orders as sosvc
    o = _so(ctx, oid)
    err = sosvc.decline(ctx.db, o, reason, ctx.user.id)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"Заявка {o.code}: отказ")
    return _to("/site/orders?tab=new")


@router.post("/site/orders/{oid}/lines/{lid}/delete")
def orders_line_delete(oid: str, lid: str, ctx: Ctx = Depends(get_ctx)):
    """Покупатель согласился без этой детали (нет в наличии) — убрать её из заявки."""
    ctx.need("site.orders")
    from ..services import site_orders as sosvc
    o = _so(ctx, oid)
    sosvc.remove_line(ctx.db, o, _uuid(lid))
    ctx.db.commit()
    return _to("/site/orders?tab=new")


@router.post("/site/orders/api-key")
def orders_api_key(ctx: Ctx = Depends(get_ctx)):
    """Ключ для сайта (только админ): с ним сайт присылает заявки. Новый ключ — старый перестаёт работать."""
    if ctx.user.role.code != "admin":
        raise Forbidden()
    import secrets
    from ..services import settings as st_
    st_.put(ctx.db, ctx.branch.id, "site_api", {"key": secrets.token_urlsafe(24)})
    ctx.db.commit()
    flash(ctx.request, "Новый ключ для сайта создан — передайте его разработчику сайта")
    return _to("/site/orders")


@router.post("/api/site/orders")
async def api_site_order(request: Request):
    """Сайт присылает заявку (JSON). Заголовок X-Site-Key — ключ из «Заявок с сайта» (создаёт админ).
    {"last_name", "first_name", "phone", "delivery": np|courier|pickup, "city", "np_branch", "address",
     "pay": cod|card|on_pickup, "comment", "items": [{"code": "Р-0001", "qty": 1, "price": 1200, "name": "…"}]}"""
    from fastapi.responses import JSONResponse

    from ..db import SessionLocal, current_branch_id, current_user_id
    from ..models import Setting
    from ..services import site_orders as sosvc
    key = (request.headers.get("x-site-key") or "").strip()
    if len(key) < 20:
        return JSONResponse({"ok": False, "error": "нет ключа"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "нужен JSON"}, status_code=400)
    with SessionLocal() as db:
        row = next((s for s in db.scalars(select(Setting).where(Setting.key == "site_api"))
                    if (s.value or {}).get("key") == key), None)
        if not row:
            return JSONResponse({"ok": False, "error": "неверный ключ"}, status_code=401)
        current_branch_id.set(row.branch_id)
        current_user_id.set(None)
        items = body.get("items") if isinstance(body.get("items"), list) else []
        res = sosvc.create(db, row.branch_id, {k: str(body.get(k) or "") for k in (
            "last_name", "first_name", "phone", "delivery", "city", "np_branch", "address", "pay", "comment")},
            [{k: str(i.get(k) or "") for k in ("code", "qty", "price", "name")} for i in items if isinstance(i, dict)], "site")
        if isinstance(res, list):
            return JSONResponse({"ok": False, "error": "не заполнено: " + ", ".join(res)}, status_code=400)
        db.commit()
        return {"ok": True, "number": res.code}


@router.post("/stock/items/{iid}/to-site")
def offer(iid: str, ctx: Ctx = Depends(get_ctx), qty: str = Form("1")):
    """«На продажу» из карточки склада (кладовщик, главный кладовщик, директор, зам, админ)."""
    ctx.need("stock.manage", "razbor.price")
    i = ctx.db.get(StockItem, _uuid(iid))
    if not i or i.branch_id != ctx.branch.id:
        raise Forbidden()
    res = svc.offer_from_stock(ctx.db, i, _dec(qty, "1"))
    if isinstance(res, str):
        flash(ctx.request, res, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"Выставлено на продажу: {res.code}, цена {res.price:.2f} ₴ за шт — ждёт утверждения директором")
    return _to(f"/stock/items/{iid}")
