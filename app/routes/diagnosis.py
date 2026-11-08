"""Шаг 4: диагностика (слесарь), согласование (менеджер), акт приёма, карта клиента."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from ..db import now
from ..models import Client, ClientEvent, User, Vehicle, WoFinding, WoPhoto
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS
from ..services import orders as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid

router = APIRouter()


def _is_mech(ctx: Ctx, wo) -> bool:
    return ctx.user.id == wo.mechanic_id or ctx.can("orders.view_all")


def _back(order_id: str):
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# ---------- Диагностика ----------
@router.post("/orders/{order_id}/findings")
async def finding_add(order_id: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Слесарь добавляет найденную проблему: текст, срочность, фото (сжатые на телефоне)."""
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    if wo.status not in ("diag", "approve", "work") or not _is_mech(ctx, wo):
        raise Forbidden()
    form = await request.form(max_files=2 * svc.MAX_PHOTOS + 2)
    text = str(form.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False, "errors": ["Опишите проблему"]}, status_code=400)
    f = svc.add_finding(ctx.db, wo, text, str(form.get("urgency", "soon")))
    photos = [p for p in form.getlist("photos") if hasattr(p, "read")][:10]
    thumbs = [p for p in form.getlist("thumbs") if hasattr(p, "read")]
    for i, p in enumerate(photos):
        thumb = await thumbs[i].read() if i < len(thumbs) else None
        svc.save_photo(ctx.db, wo, await p.read(), thumb, "diag", finding_id=f.id)
    svc.event(ctx.db, wo, "finding", f.text, finding=f, urgency=f.urgency)
    if wo.status == "work":  # нашли новое во время ремонта — снова к клиенту на согласование
        svc.set_status(ctx.db, wo, "approve", "Во время работы найдена новая проблема")
    ctx.db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/findings/{finding_id}/delete")
def finding_delete(order_id: str, finding_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    f = ctx.db.get(WoFinding, _uuid(finding_id))
    if (f and f.work_order_id == wo.id and f.approval == "pending" and wo.status in ("diag", "approve")
            and (f.created_by == ctx.user.id or ctx.can("orders.view_all"))):
        for p in ctx.db.scalars(select(WoPhoto).where(WoPhoto.finding_id == f.id)):
            p.finding_id = None  # фото остаются в наряде
        for e in ctx.db.scalars(select(ClientEvent).where(ClientEvent.finding_id == f.id)):
            ctx.db.delete(e)
        ctx.db.delete(f)
        ctx.db.commit()
    return _back(order_id)


@router.post("/orders/{order_id}/diag-done")
def diag_done(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    if wo.status == "diag" and _is_mech(ctx, wo):
        n = len(ctx.db.scalars(select(WoFinding.id).where(WoFinding.work_order_id == wo.id)).all())
        svc.finish_diagnosis(ctx.db, wo, n)
        ctx.db.commit()
        flash(ctx.request, "Диагностика отправлена менеджеру на согласование")
    return _back(order_id)


# ---------- Согласование ----------
@router.post("/orders/{order_id}/findings/{finding_id}/approval")
def finding_approval(order_id: str, finding_id: str, ctx: Ctx = Depends(get_ctx), value: str = Form(...)):
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    f = ctx.db.get(WoFinding, _uuid(finding_id))
    if wo.status != "approve" or not f or f.work_order_id != wo.id or value not in ("yes", "no", "pending"):
        return JSONResponse({"ok": False}, status_code=400)
    f.approval, f.approved_by, f.approved_at = value, ctx.user.id, now()
    ctx.db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/approval-done")
def approval_done(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    if wo.status == "approve":
        err = svc.finish_approval(ctx.db, wo)
        if err:
            ctx.db.rollback()
            flash(ctx.request, err, "error")
        else:
            ctx.db.commit()
            flash(ctx.request, "Согласование завершено")
    return _back(order_id)


# ---------- Акт приёма (печать) ----------
@router.get("/orders/{order_id}/act/intake")
def act_intake(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.view_all", "orders.view_own")
    wo = _order(ctx, order_id)
    db = ctx.db
    n_photos = len(db.scalars(select(WoPhoto.id).where(WoPhoto.work_order_id == wo.id, WoPhoto.stage == "intake")).all())
    return ctx.render("orders/act_intake.html", title=f"Акт приёма {wo.number}", wo=wo, v=db.get(Vehicle, wo.vehicle_id),
                      c=db.get(Client, wo.client_id), manager=db.get(User, wo.manager_id) if wo.manager_id else None,
                      n_photos=n_photos, INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
                      fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate)


# ---------- Карта клиента: заметки и рекомендации ----------
@router.post("/clients/{client_id}/note")
def client_note(client_id: str, ctx: Ctx = Depends(get_ctx), text: str = Form("")):
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id:
        raise Forbidden()
    if text.strip():
        svc.event(ctx.db, None, "note", text.strip(), client_id=c.id)
        ctx.db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/events/{event_id}/resolve")
def recommendation_resolve(client_id: str, event_id: str, ctx: Ctx = Depends(get_ctx)):
    """Рекомендацию выполнили (или она больше не актуальна) — и обратно."""
    ctx.need("clients.view")
    e = ctx.db.get(ClientEvent, _uuid(event_id))
    if e and str(e.client_id) == client_id and e.branch_id == ctx.branch.id and e.kind == "recommendation":
        e.resolved = not e.resolved
        ctx.db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
