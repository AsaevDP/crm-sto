"""Мойка и детейлинг (12.11.2026, решение брата).

«Мойка» (менеджер, директор, зам — право wash.view): какие машины на мойке и по какому наряду, химия на складе,
кто сколько взял, «Закупка для мойки» (право wash.buy — заказать химию у поставщика, дальше как обычная закупка).
«Химия» (мойщик, детейлер — право chem.ask): что есть на складе, «Попросить» → кладовщик выдаёт.
Химия — расход СТО, входит в стоимость мойки, в счёт клиенту не идёт.
"""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select

from ..models import ChemRequest, Location, PartCatalog, Purchase, PurchaseLine, Supplier, User, Vehicle, WorkOrder, WoWork
from ..models.orders import WO_ACTIVE
from ..permissions import MASTERS, WASHERS
from ..services import chem as csvc
from ..services import orders as osvc
from ..services import stock as ssvc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()
LOW = Decimal(2)   # мало химии — «пора заказать»


def _to(url):
    return RedirectResponse(url, status_code=303)


def _dec(v, default="1") -> Decimal:
    try:
        return Decimal(str(v or default).replace(",", ".").replace(" ", ""))
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
        .order_by(WorkOrder.created_at)).all()
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
    cars = on_wash(db, bid)
    items = csvc.items(db, bid)
    asks = csvc.requests(db, bid, "new")
    chem_p = db.scalars(select(Purchase).where(Purchase.branch_id == bid, Purchase.kind == "chem")
                        .order_by(Purchase.created_at.desc()).limit(100)).all()
    if tab not in ("now", "chem", "buy"):
        tab = "now"
    low = [x for x in items if x["free"] <= LOW]
    data = dict(tab=tab, cars=cars, items=items, asks=asks, low=low, WASH_ST=WASH_ST, fmt_plate=osvc.fmt_plate,
                n_open=sum(1 for p in chem_p if p.status != "arrived"),
                supplier_list=db.scalars(select(Supplier).where(Supplier.branch_id == bid).order_by(Supplier.name)).all())
    if tab == "chem":
        data.update(usage=csvc.usage_by_person(db, bid, 30), done=csvc.requests(db, bid, ("issued", "declined"), days=30)[:40],
                    locs={l.id: ssvc.location_label(l) for l in db.scalars(select(Location).where(Location.branch_id == bid))},
                    cost_ok=ctx.can("prices.view_purchase"))
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
