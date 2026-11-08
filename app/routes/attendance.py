"""Табель и проходная.

Проходная (/kiosk) работает БЕЗ входа в CRM — но только на устройстве, которое главный админ
сделал проходной (подписанная кука с точкой доступа). Чужой браузер отметки ставить не может.
"""
import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import COOKIE_SECURE, SECRET_KEY
from ..db import current_branch_id, current_user_id, get_db
from ..models import AccessPoint, AttendanceEvent, User
from ..services import attendance as svc
from ..services import settings as st
from ..web import Ctx, Forbidden, accent_style, flash, get_ctx, templates
from .orders import _uuid

router = APIRouter()
KIOSK_COOKIE = "crm_kiosk"
_signer = URLSafeSerializer(SECRET_KEY, salt="kiosk")
MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _kiosk_point(request: Request, db: Session) -> AccessPoint | None:
    raw = request.cookies.get(KIOSK_COOKIE)
    if not raw:
        return None
    try:
        pid = _signer.loads(raw)
    except BadSignature:
        return None
    return db.get(AccessPoint, uuid.UUID(pid))


# ======================= Проходная =======================
@router.get("/kiosk")
def kiosk(request: Request, db: Session = Depends(get_db)):
    point = _kiosk_point(request, db)
    branding = st.get(db, point.branch_id, "branding") if point else {"name": "CRM СТО", "accent": "blue"}
    return templates.TemplateResponse(request, "attendance/kiosk.html", {
        "point": point, "branding": branding, "accent": accent_style(branding.get("accent", "blue"))})


@router.post("/kiosk/tap")
def kiosk_tap(request: Request, uid: str = Form(""), db: Session = Depends(get_db)):
    point = _kiosk_point(request, db)
    if not point:
        return JSONResponse({"ok": False, "error": "Устройство не настроено как проходная"}, status_code=403)
    current_branch_id.set(point.branch_id)
    res = svc.tap(db, point.branch_id, uid, point)
    if isinstance(res, str):
        return JSONResponse({"ok": False, "error": res}, status_code=404)
    user, ev = res
    current_user_id.set(user.id)
    db.commit()
    at = svc._aware(ev.at).astimezone(svc.local_tz())
    return {"ok": True, "name": user.full_name, "role": user.role.name, "direction": ev.direction,
            "time": at.strftime("%H:%M"), "initials": user.initials}


# ======================= Точки доступа (настройки) =======================
@router.get("/settings/points")
def points(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    rows = ctx.db.scalars(select(AccessPoint).where(AccessPoint.branch_id == ctx.branch.id).order_by(AccessPoint.name)).all()
    here = _kiosk_point(ctx.request, ctx.db)
    return ctx.render("attendance/points.html", title="Проходные", rows=rows, here=here)


@router.post("/settings/points")
def point_add(ctx: Ctx = Depends(get_ctx), name: str = Form(...), counts: str = Form("1")):
    ctx.need("settings.access")
    if name.strip():
        ctx.db.add(AccessPoint(name=name.strip()[:80], kind="gate", counts_presence=counts == "1"))
        ctx.db.commit()
    return RedirectResponse("/settings/points", status_code=303)


@router.post("/settings/points/{pid}/kiosk")
def point_kiosk(pid: str, ctx: Ctx = Depends(get_ctx)):
    """Сделать ЭТО устройство проходной (только главный админ)."""
    if ctx.user.role.code != "admin":
        raise Forbidden()
    p = ctx.db.get(AccessPoint, _uuid(pid))
    if not p or p.branch_id != ctx.branch.id:
        raise Forbidden()
    r = RedirectResponse("/kiosk", status_code=303)
    r.set_cookie(KIOSK_COOKIE, _signer.dumps(str(p.id)), max_age=5 * 365 * 24 * 3600, httponly=True,
                 samesite="lax", secure=COOKIE_SECURE)
    return r


@router.post("/settings/points/unkiosk")
def point_unkiosk(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    r = RedirectResponse("/settings/points", status_code=303)
    r.delete_cookie(KIOSK_COOKIE)
    return r


# ======================= Табель =======================
def _month(m: str):
    try:
        y, mo = (int(x) for x in m.split("-"))
        return y, mo
    except (ValueError, AttributeError):
        t = date.today()
        return t.year, t.month


@router.get("/timesheet")
def timesheet(ctx: Ctx = Depends(get_ctx), m: str = "", user: str = ""):
    ctx.need("attendance.view_all", "attendance.view_own")
    import calendar
    y, mo = _month(m)
    n_days = calendar.monthrange(y, mo)[1]
    prev = (date(y, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    nxt = (date(y, mo, n_days) + timedelta(days=1)).strftime("%Y-%m")
    see_all = ctx.can("attendance.view_all")
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True)).order_by(User.full_name)).all()
    target = None
    if not see_all:
        target = ctx.user
    elif user:
        target = ctx.db.get(User, _uuid(user))
    if target:
        grid = svc.month_grid(ctx.db, ctx.branch.id, [target], y, mo)[target.id]
        return ctx.render("attendance/user.html", title="Табель", u=target, grid=grid, y=y, mo=mo, n_days=n_days,
                          month_name=MONTHS[mo], prev=prev, nxt=nxt, see_all=see_all, date=date,
                          total=sum(d.minutes for d in grid.values()), can_edit=ctx.user.role.code == "admin",
                          local=lambda e: svc._aware(e.at).astimezone(svc.local_tz()))
    grid = svc.month_grid(ctx.db, ctx.branch.id, staff, y, mo)
    now_on = svc.on_site_now(ctx.db, ctx.branch.id, staff)
    weekends = {d for d in range(1, n_days + 1) if date(y, mo, d).weekday() >= 5}
    return ctx.render("attendance/month.html", title="Табель", staff=staff, grid=grid, now_on=now_on, y=y, mo=mo,
                      n_days=n_days, month_name=MONTHS[mo], prev=prev, nxt=nxt, weekends=weekends,
                      today=date.today(), local=lambda e: svc._aware(e.at).astimezone(svc.local_tz()))


@router.post("/timesheet/events")
def event_add(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), day: str = Form(...), at: str = Form(...),
              direction: str = Form("in"), comment: str = Form("")):
    """Ручная отметка (забыл карту, не отметил уход) — только главный админ, с комментарием."""
    if ctx.user.role.code != "admin":
        raise Forbidden()
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id or direction not in ("in", "out"):
        raise Forbidden()
    if not comment.strip():
        flash(ctx.request, "Ручная отметка: укажите причину", "error")
        return RedirectResponse(f"/timesheet?user={user_id}&m={day[:7]}", status_code=303)
    try:
        dt = datetime.combine(date.fromisoformat(day), time.fromisoformat(at), tzinfo=svc.local_tz())
    except ValueError:
        flash(ctx.request, "Проверьте дату и время", "error")
        return RedirectResponse(f"/timesheet?user={user_id}", status_code=303)
    from datetime import timezone
    ctx.db.add(AttendanceEvent(user_id=u.id, direction=direction, at=dt.astimezone(timezone.utc), method="manual",
                               comment=comment.strip()[:300]))
    ctx.db.commit()
    flash(ctx.request, "Отметка добавлена")
    return RedirectResponse(f"/timesheet?user={user_id}&m={day[:7]}", status_code=303)


@router.post("/timesheet/events/{eid}/delete")
def event_delete(eid: str, ctx: Ctx = Depends(get_ctx)):
    if ctx.user.role.code != "admin":
        raise Forbidden()
    e = ctx.db.get(AttendanceEvent, _uuid(eid))
    if not e or e.branch_id != ctx.branch.id:
        raise Forbidden()
    uid, m = e.user_id, svc._aware(e.at).astimezone(svc.local_tz()).strftime("%Y-%m")
    ctx.db.delete(e)
    ctx.db.commit()
    flash(ctx.request, "Отметка удалена (запись в журнале осталась)")
    return RedirectResponse(f"/timesheet?user={uid}&m={m}", status_code=303)
