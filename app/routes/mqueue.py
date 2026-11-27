"""«Наряды → Очередь мастеров» (27.11.2026, брат): колонка на мастера, менеджер / директор / зам / админ двигают
↑ ↓ «Первым» «В конец»; мастер видит только смотрит и просит «Попросить первой» в режиме «Обязательная»."""
import uuid

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import MasterQueueRequest, User, WorkOrder
from ..permissions import is_master
from ..services import master_queue as mq
from ..services import orders as osvc
from ..web import Ctx, Forbidden, flash, get_ctx

router = APIRouter()
BOSSES = ("director", "deputy", "admin")


def _uuid(v: str):
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        raise Forbidden()


def _back(back: str, default: str = "/orders/queue"):
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else default, status_code=303)


def can_see(ctx: Ctx) -> bool:
    return ctx.can("schedule.queue") or (is_master(ctx.user) and ctx.user.role.code in mq.ROLES)


@router.get("/orders/queue")
def queue_page(ctx: Ctx = Depends(get_ctx), k: str = "all"):
    if not can_see(ctx):
        raise Forbidden()
    k = k if k in mq.KINDS else "all"
    data = mq.build(ctx.db, ctx.branch.id, k)
    from ..models import Vehicle
    asks = []
    if ctx.can("schedule.queue"):
        for r in mq.open_asks(ctx.db, ctx.branch.id):
            wo = ctx.db.get(WorkOrder, r.work_order_id)
            asks.append({"r": r, "wo": wo, "v": ctx.db.get(Vehicle, wo.vehicle_id), "m": ctx.db.get(User, r.mechanic_id)})
    present = {m.role.code for m in mq.masters(ctx.db, ctx.branch.id)}
    return ctx.render("orders/queue.html", title="Очередь мастеров", data=data, k=k, KINDS=mq.KINDS, present=present,
                      edit=ctx.can("schedule.queue"), boss=ctx.user.role.code in BOSSES, asks=asks,
                      fmt_min=mq.fmt_min, when_word=mq.when_word, fmt_plate=osvc.fmt_plate)


@router.post("/orders/queue/{wid}/move")
def queue_move(wid: str, ctx: Ctx = Depends(get_ctx), mech: str = Form(...), where: str = Form("up"), back: str = Form("")):
    ctx.need("schedule.queue")
    mid = _uuid(mech)
    if mid not in {m.id for m in mq.masters(ctx.db, ctx.branch.id)}:
        raise Forbidden()
    wo = ctx.db.get(WorkOrder, _uuid(wid))
    if not wo or wo.branch_id != ctx.branch.id:
        raise Forbidden()
    if mq.move_and_tell(ctx.db, ctx.branch.id, mid, wo.id, where, ctx.user):
        ctx.db.commit()
        word = {"up": "выше", "down": "ниже", "first": "первым", "last": "в конец"}.get(where, "")
        flash(ctx.request, f"Наряд {wo.number[-6:]} — {word}")
    return _back(back)


@router.post("/orders/queue/mode")
def queue_mode(ctx: Ctx = Depends(get_ctx), mech: str = Form(...), strict: str = Form(""), back: str = Form("")):
    """Режим мастера «Подсказка / Обязательная» — директор, зам, админ."""
    if ctx.user.role.code not in BOSSES:
        raise Forbidden()
    m = ctx.db.get(User, _uuid(mech))
    if not m or m.branch_id != ctx.branch.id or m.role.code not in mq.ROLES:
        raise Forbidden()
    mq.set_strict(ctx.db, ctx.branch.id, m.id, bool(strict))
    ctx.db.commit()
    flash(ctx.request, f"{m.short_name}: очередь — {'обязательная' if strict else 'подсказка'}")
    return _back(back)


@router.post("/orders/queue/{wid}/ask")
def queue_ask(wid: str, ctx: Ctx = Depends(get_ctx), reason: str = Form(""), back: str = Form("/orders#myq")):
    if not is_master(ctx.user) or ctx.user.role.code not in mq.ROLES:
        raise Forbidden()
    wo = ctx.db.get(WorkOrder, _uuid(wid))
    if not wo or wo.branch_id != ctx.branch.id:
        raise Forbidden()
    err = mq.ask(ctx.db, wo, ctx.user, reason)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Запрос отправлен менеджеру — ответ придёт сообщением")
    return _back(back, "/orders#myq")


@router.post("/orders/queue/asks/{rid}")
def queue_decide(rid: str, ctx: Ctx = Depends(get_ctx), action: str = Form(...), answer: str = Form(""),
                 back: str = Form("/orders/queue#asks")):
    ctx.need("schedule.queue")
    r = ctx.db.get(MasterQueueRequest, _uuid(rid))
    if not r or r.branch_id != ctx.branch.id:
        raise Forbidden()
    err = mq.decide(ctx.db, r, ctx.user, action == "ok", answer)
    if err:
        flash(ctx.request, err, "error")
    else:
        ctx.db.commit()
        flash(ctx.request, "Машина поставлена первой" if action == "ok" else "Отказано — мастеру отправлен ответ")
    return _back(back, "/orders/queue#asks")
