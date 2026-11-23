"""Шаг 4: диагностика (слесарь), согласование (менеджер), акт приёма, карта клиента."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from ..db import now
from ..models import Client, ClientEvent, User, Vehicle, WoFinding, WoPhoto
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, URGENCY
from ..services import orders as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _order, _uuid

router = APIRouter()


def _is_mech(ctx: Ctx, wo) -> bool:
    return ctx.user.id == wo.mechanic_id or ctx.can("orders.view_all")


# 23.11.2026 (брат): новая проблема — на любом этапе после согласования до выдачи (и когда машина «Готово»): наряд
# снова «Согласование», мастера продолжают согласованное, запчастист подбирает к новому, менеджер звонит ещё раз
NEW_PROBLEM_FROM = ("picking", "waiting", "instock", "work", "ready")


def may_add_finding(ctx: Ctx, wo) -> bool:
    """Кто может записать проблему. Мастер наряда (ведущий, добавленный, с работой в наряде) — диагностика, согласование
    и после согласования до выдачи. Менеджер — то же плюс сразу после приёмки (со слов клиента / со слов мастера)."""
    if not ctx.can("orders.diagnose") or (wo.kind or "") == "wash":
        return False
    from ..models import WoWork
    from ..services import handover as hsvc
    st = wo.status
    if ctx.can("orders.approve") and st in ("created", "diag", "approve", *NEW_PROBLEM_FROM):
        return True
    master = (_is_mech(ctx, wo) or hsvc.is_extra(ctx.db, wo, ctx.user.id) is not None
              or ctx.db.scalar(select(WoWork.id).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id == ctx.user.id)
                               .limit(1)) is not None)
    return master and st in ("diag", "approve", *NEW_PROBLEM_FROM)


def mgr_turn(db, wo) -> bool:
    """Мастера закончили диагностику и передали менеджеру: «проблемы впишет менеджер»."""
    if wo.status != "diag" or not wo.problems_by_manager:
        return False
    from ..services import handover as hsvc
    return all(s["done"] for s in hsvc.diag_state(db, wo))


def _back(order_id: str):
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# ---------- Диагностика ----------
@router.post("/orders/{order_id}/findings")
async def finding_add(order_id: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Слесарь добавляет найденную проблему: текст, срочность, фото (сжатые на телефоне)."""
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    if not may_add_finding(ctx, wo):   # 23.11.2026: правила — may_add_finding (и после согласования до выдачи)
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
    if wo.status in NEW_PROBLEM_FROM:  # нашли новое после согласования — снова к клиенту; запчастист подбирает к новому
        from ..models.orders import WO_STATUSES
        svc.set_status(ctx.db, wo, "approve", f"Новая проблема (было: {WO_STATUSES.get(wo.status, wo.status)})")
        svc.new_problem_notify(ctx.db, wo, f, ctx.user)
    ctx.db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/findings/{finding_id}/delete")
def finding_delete(order_id: str, finding_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    f = ctx.db.get(WoFinding, _uuid(finding_id))
    if (f and f.work_order_id == wo.id and f.approval == "pending" and wo.status in ("created", "diag", "approve")
            and (f.created_by == ctx.user.id or ctx.can("orders.view_all"))):
        for p in ctx.db.scalars(select(WoPhoto).where(WoPhoto.finding_id == f.id)):
            p.finding_id = None  # фото остаются в наряде
        for e in ctx.db.scalars(select(ClientEvent).where(ClientEvent.finding_id == f.id)):
            ctx.db.delete(e)
        ctx.db.delete(f)
        ctx.db.commit()
    return _back(order_id)


@router.post("/orders/{order_id}/findings/{finding_id}/edit")
def finding_edit(order_id: str, finding_id: str, ctx: Ctx = Depends(get_ctx), text: str = Form(""),
                 urgency: str = Form("")):
    """22.11.2026 (брат): исправить проблему, не удаляя — текст и срочность. Кто написал и менеджер, пока наряд не
    согласован с клиентом. Работа, созданная из проблемы («✓ Да»), меняет название, если его не правили отдельно."""
    wo = _order(ctx, order_id)
    f = ctx.db.get(WoFinding, _uuid(finding_id))
    if not f or f.work_order_id != wo.id:
        raise Forbidden()
    if wo.status not in ("created", "diag", "approve") or not (
            f.created_by == ctx.user.id or ctx.can("orders.approve") or ctx.can("orders.view_all")):
        flash(ctx.request, "Проблему уже нельзя изменить — наряд согласован с клиентом", "error")
        return _back(order_id)
    text = text.strip()
    if not text:
        flash(ctx.request, "Опишите проблему", "error")
        return _back(order_id)
    old = f.text
    f.text = text[:2000]
    if urgency in URGENCY:
        f.urgency = urgency
    from ..models import WoWork
    w = ctx.db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.finding_id == f.id))
    if w and w.name == old[:200]:
        w.name = f.text[:200]
    ctx.db.commit()
    flash(ctx.request, "Проблема исправлена")
    return _back(order_id)


@router.post("/orders/{order_id}/diag-done")
def diag_done(order_id: str, ctx: Ctx = Depends(get_ctx), by_manager: str = Form("")):
    """«Диагностика готова» (13.11.2026, брат): мастеров несколько — каждый отмечает свою; на согласование наряд уходит,
    когда закончили все. 23.11.2026: «Диагностика готова, проблемы впишет менеджер» (by_manager) — мастер продиктовал /
    написал на бумаге; когда закончили все, наряд ждёт менеджера (🔴 и окошко), он вписывает и жмёт «На согласование»."""
    ctx.need("orders.diagnose")
    wo = _order(ctx, order_id)
    from ..services import handover as hsvc
    extra = hsvc.is_extra(ctx.db, wo, ctx.user.id)
    if wo.status == "diag" and (extra or ctx.user.id == wo.mechanic_id):   # только сам мастер — свою диагностику
        if by_manager:
            wo.problems_by_manager = True
            svc.event(ctx.db, wo, "status", f"Диагностика готова ({ctx.user.short_name}) — проблемы впишет менеджер")
        all_done = hsvc.mark_diag_done(ctx.db, wo, ctx.user.id)
        if all_done and wo.problems_by_manager:
            from ..services import notify
            notify.send(ctx.db, [wo.manager_id], "diag_to_mgr", f"Диагностика готова — впишите проблемы · {wo.number}",
                        f"{notify.car(db=ctx.db, wo=wo)} · проблемы со слов мастера: {ctx.user.short_name}",
                        f"/orders/{wo.id}#problems", wo, skip=ctx.user.id)
            flash(ctx.request, "Диагностика готова — проблемы впишет менеджер, ему отправлено сообщение")
            ctx.db.commit()
            return _back(order_id)
        if all_done:
            n = len(ctx.db.scalars(select(WoFinding.id).where(WoFinding.work_order_id == wo.id)).all())
            svc.finish_diagnosis(ctx.db, wo, n)
            flash(ctx.request, "Диагностика отправлена менеджеру на согласование")
        else:
            left = [s["user"].short_name for s in hsvc.diag_state(ctx.db, wo) if not s["done"] and s["user"]]
            flash(ctx.request, "Ваша диагностика готова. Ждём остальных мастеров: " + ", ".join(left))
        ctx.db.commit()
    return _back(order_id)


@router.post("/orders/{order_id}/to-diag")
def to_diag(order_id: str, ctx: Ctx = Depends(get_ctx), mechanic_id: str = Form(""), lift_id: str = Form("")):
    """Менеджер отправляет машину на диагностику: слесарь (и пост) → статус «Диагностика»."""
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    mech = ctx.db.get(User, _uuid(mechanic_id)) if mechanic_id else None
    if wo.status != "created" or not mech or mech.branch_id != ctx.branch.id:
        flash(ctx.request, "Выберите слесаря", "error")
        return _back(order_id)
    wo.mechanic_id = mech.id
    if lift_id:
        from ..models import Lift
        lift = ctx.db.get(Lift, _uuid(lift_id))
        if lift and lift.branch_id == ctx.branch.id:
            wo.lift_id = lift.id
    svc.set_status(ctx.db, wo, "diag", f"На диагностику: {mech.full_name}")
    ctx.db.commit()
    flash(ctx.request, f"Машина отправлена на диагностику: {mech.full_name}")
    return _back(order_id)


@router.post("/orders/{order_id}/to-approve")
def to_approve(order_id: str, ctx: Ctx = Depends(get_ctx)):
    """Менеджер записал проблемы со слов клиента — сразу к согласованию, без диагностики."""
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    n = len(ctx.db.scalars(select(WoFinding.id).where(WoFinding.work_order_id == wo.id)).all())
    after_master = mgr_turn(ctx.db, wo)   # 23.11.2026: мастер закончил, проблемы со слов мастера вписал менеджер
    if not (wo.status == "created" or after_master) or not n:
        flash(ctx.request, "Сначала запишите хотя бы одну проблему", "error")
        return _back(order_id)
    wo.problems_by_manager = False
    svc.set_status(ctx.db, wo, "approve", f"Проблемы {'со слов мастера ' if after_master else ''}записал менеджер: {n}")
    svc.event(ctx.db, wo, "status", f"Проблемы со слов клиента: {n}")
    ctx.db.commit()
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
    svc.work_for_finding(ctx.db, wo, f, value == "yes")   # 13.11.2026: «Да» — сразу работа в наряде (цена, мастер)
    ctx.db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/approval-done")
def approval_done(order_id: str, ctx: Ctx = Depends(get_ctx), no_parts: str = Form(""), dups_ok: str = Form("")):
    """«Согласовал с клиентом» (13.11.2026): проблемы, работы и запчасти разом — после «Подбор завершён».
    19.11.2026: одна деталь несколько раз — нужна галочка «Да, клиент берёт все» (dups_ok)."""
    ctx.need("orders.approve")
    wo = _order(ctx, order_id)
    if wo.status == "approve":
        err = svc.finish_approval(ctx.db, wo, no_parts=bool(no_parts), dups_ok=bool(dups_ok))
        if err:
            ctx.db.rollback()
            flash(ctx.request, err, "error")
        else:
            ctx.db.commit()
            from ..models.orders import WO_STATUSES
            flash(ctx.request, f"Согласовано с клиентом — {WO_STATUSES.get(wo.status, wo.status)}")
    return _back(order_id)


# ---------- Акт приёма (печать) ----------
@router.get("/orders/{order_id}/act/intake")
def act_intake(order_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("orders.view_all", "orders.view_own")
    wo = _order(ctx, order_id)
    db = ctx.db
    photos = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id, WoPhoto.stage == "intake")
                        .order_by(WoPhoto.created_at)).all()
    from ..models import Lift, WoWork
    works = db.scalars(select(WoWork.name).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all()
    return ctx.render("orders/act_intake.html", title=f"Акт приёма {wo.number}", wo=wo, v=db.get(Vehicle, wo.vehicle_id),
                      c=db.get(Client, wo.client_id), manager=db.get(User, wo.manager_id) if wo.manager_id else None,
                      mechanic=db.get(User, wo.mechanic_id) if wo.mechanic_id else None,
                      lift=db.get(Lift, wo.lift_id) if wo.lift_id else None,
                      photo=photos[0] if photos else None, n_photos=len(photos), works=works,
                      INTAKE_ITEMS=INTAKE_ITEMS, FUEL_LEVELS=FUEL_LEVELS,
                      fmt_phone=svc.fmt_phone, fmt_plate=svc.fmt_plate)


# ---------- Карта клиента: заметки и рекомендации ----------
@router.post("/clients/{client_id}/note")
def client_note(client_id: str, ctx: Ctx = Depends(get_ctx), text: str = Form("")):
    ctx.need("clients.view")
    c = ctx.db.get(Client, _uuid(client_id))
    if not c or c.branch_id != ctx.branch.id or not svc.can_see_client(ctx, c.id):
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
    if e and str(e.client_id) == client_id and e.branch_id == ctx.branch.id and e.kind == "recommendation" and svc.can_see_client(ctx, e.client_id):
        e.resolved = not e.resolved
        ctx.db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
