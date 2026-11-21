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
    # Б/у · разбор — то, что уходит на сайт из разбора (как плитка «На сайте» в «Разборе»)
    from .razbor import _item_rows
    used = db.scalars(select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == bid, PartCatalog.kind == "used", StockItem.publish_on_site.is_(True))
        .order_by(StockItem.code.desc())).all()
    used_rows = [r for r in _item_rows(ctx, used) if r["state"] == "stock"]
    t = t if t in ("new", "used") else ("new" if (counts["wait"] and ctx.can("razbor.price")) or not used_rows else "new")
    if f not in svc.STAGES:
        f = next((k for k in ("wait", "declined", "site", "nostock") if counts[k]), "site") if t == "new" else ""
    shown = [r for r in new_rows if r["stage"] == f] if t == "new" else []
    return ctx.render("site/index.html", title="На сайт", t=t, f=f, rows=shown, counts=counts, STAGES=svc.STAGES,
                      used_rows=used_rows, n_new_site=counts["site"], n_used=len(used_rows),
                      can_price=ctx.can("razbor.price"), can_edit=ctx.can("razbor.export"),
                      can_sell=ctx.can("razbor.sell"), can_export=ctx.can("razbor.export"),
                      CHANNELS=CHANNELS, PAY_METHODS=PAY_METHODS, fmt_phone=osvc.fmt_phone)


@router.post("/site/new/{sid}/approve")
def approve(sid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("razbor.price")
    s = _sn(ctx, sid)
    err = svc.approve(ctx.db, s, ctx.user)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, f"{s.code}: цена {s.price:.2f} ₴ утверждена — деталь на сайте")
    return _to("/site?t=new&f=wait")


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
            flash(ctx.request, "Фото удалено")
    else:
        s.description = str(form.get("description", "")).strip()[:2000]
        s.publish = form.get("publish") == "1"
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
