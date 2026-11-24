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
from ..services import lateness as late_svc
from ..services import settings as st
from ..services.staff import staff_for_period
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


def _alerts(ctx: Ctx, only: User | None = None) -> dict:
    """Окошки табеля (24.11.2026, брат). lt_me — свои (опоздание, ⚠ не отметились, «что делали после работы»),
    lt_boss — руководству «Нужно решить» (кнопки — attendance.decide: директор, зам, админ; бухгалтер видит)."""
    from ..models.staff import ATT_DECISIONS, ATT_REASONS
    me = ctx.user
    mine = late_svc.for_user(ctx.db, ctx.branch.id, me) if (only is None or only.id == me.id) else None
    boss = late_svc.for_boss(ctx.db, ctx.branch.id, me) if ctx.can("attendance.alerts", "attendance.decide") else []
    if only is not None:
        boss = [x for x in boss if x["f"].user.id == only.id]
    for x in boss:
        if x["f"].kind == "overtime":
            x["works"] = late_svc.closed_orders_after(ctx.db, x["f"].user, x["f"])
    return {"lt_me": mine, "lt_boss": boss, "lt_kind": late_svc.KIND_NAME, "lt_min": late_svc.fmt_min,
            "lt_decide": ctx.can("attendance.decide"), "ATT_DECISIONS": ATT_DECISIONS, "ATT_REASONS": ATT_REASONS,
            "lt_p": late_svc.params(ctx.db, ctx.branch.id)}


def _back(back: str, default: str = "/timesheet#boss"):
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else default, status_code=303)


@router.post("/timesheet/ack")
def ack(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), day: str = Form(...), kind: str = Form(...),
        side: str = Form("self"), back: str = Form("/timesheet#late")):
    """«Принял к сведению»: self — сотрудник о своём опоздании; boss — руководство о «не отметил уход» (один за всех)."""
    uid = _uuid(user_id)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise Forbidden()
    if side == "self":
        if uid != ctx.user.id or kind != "late":
            raise Forbidden()
    else:
        ctx.need("attendance.alerts", "attendance.decide")
        if uid == ctx.user.id or kind != "no_out":
            raise Forbidden()   # о себе руководитель не решает — примет другой
        u = ctx.db.get(User, uid)
        if not u or u.branch_id != ctx.branch.id:
            raise Forbidden()
    if not late_svc.ack(ctx.db, ctx.branch.id, ctx.user, uid, d, kind, side):
        raise Forbidden()
    ctx.db.commit()
    flash(ctx.request, "Принято к сведению" if side == "boss" else "Принято. Больше не опаздывайте")
    return _back(back, "/timesheet")


@router.post("/timesheet/decide")
def decide(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), day: str = Form(...), kind: str = Form(...),
           decision: str = Form(...), reason: str = Form(""), comment: str = Form(""), fine: str = Form(""),
           bonus: str = Form(""), back: str = Form("/timesheet#boss")):
    """Директор / зам / админ: решение по опозданию, неявке, раннему уходу, задержке (деньги — в зарплату недели)."""
    from decimal import Decimal, InvalidOperation
    ctx.need("attendance.decide")
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    if not late_svc.can_decide(ctx.user, u):
        flash(ctx.request, "О себе решает другой руководитель", "error")
        return _back(back)
    try:
        d = date.fromisoformat(day)
        fine_v = Decimal(fine.replace(",", ".").replace(" ", "")) if fine.strip() else None
        bonus_v = Decimal(bonus.replace(",", ".").replace(" ", "")) if bonus.strip() else None
    except (ValueError, InvalidOperation):
        flash(ctx.request, "Проверьте дату и суммы", "error")
        return _back(back)
    err = late_svc.decide(ctx.db, ctx.branch.id, ctx.user, u, d, kind, decision, reason, comment, fine_v, bonus_v)
    if err:
        ctx.db.rollback()
        flash(ctx.request, err, "error")
        return _back(back)
    ctx.db.commit()
    from ..models.staff import ATT_DECISIONS
    flash(ctx.request, f"{u.short_name}: {ATT_DECISIONS[decision].lower()}")
    return _back(back)


@router.post("/timesheet/note")
def note(ctx: Ctx = Depends(get_ctx), day: str = Form(...), text: str = Form(""), back: str = Form("/timesheet#late")):
    """Сотрудник: «что делали после работы» — руководство решит про премию."""
    ctx.need("attendance.view_own", "attendance.view_all")
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise Forbidden()
    err = late_svc.set_note(ctx.db, ctx.user, d, text)
    if err:
        flash(ctx.request, err, "error")
        return _back(back, "/timesheet")
    ctx.db.commit()
    flash(ctx.request, "Записано — руководство увидит")
    return _back(back, "/timesheet")


@router.post("/timesheet/leave")
def leave_add(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), day: str = Form(...), t_from: str = Form("08:00"),
              t_to: str = Form("19:00"), comment: str = Form(""), back: str = Form("/timesheet")):
    """«Отпросился заранее» — директор / зам / админ (в «Табеле» и на «Пульте»)."""
    from ..models import AttendanceLeave
    ctx.need("attendance.decide")
    u = ctx.db.get(User, _uuid(user_id))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    try:
        d = date.fromisoformat(day)
        a, b = time.fromisoformat(t_from), time.fromisoformat(t_to)
    except ValueError:
        flash(ctx.request, "Проверьте день и время", "error")
        return _back(back, "/timesheet")
    if b <= a:
        flash(ctx.request, "«До» должно быть позже «с»", "error")
        return _back(back, "/timesheet")
    if not comment.strip():
        flash(ctx.request, "Напишите, почему отпросился", "error")
        return _back(back, "/timesheet")
    ctx.db.add(AttendanceLeave(user_id=u.id, day=d, t_from=a.strftime("%H:%M"), t_to=b.strftime("%H:%M"),
                               comment=comment.strip()[:500]))
    ctx.db.commit()
    flash(ctx.request, f"{u.short_name}: отпросился {d:%d.%m} с {a:%H:%M} до {b:%H:%M}")
    return _back(back, "/timesheet")


@router.post("/timesheet/leave/{lid}/delete")
def leave_delete(lid: str, ctx: Ctx = Depends(get_ctx), back: str = Form("/timesheet")):
    from ..models import AttendanceLeave
    ctx.need("attendance.decide")
    lv = ctx.db.get(AttendanceLeave, _uuid(lid))
    if not lv or lv.branch_id != ctx.branch.id:
        raise Forbidden()
    ctx.db.delete(lv)
    ctx.db.commit()
    flash(ctx.request, "Отметка «отпросился» убрана (в журнале осталась)")
    return _back(back, "/timesheet")


@router.get("/settings/timesheet")
def ts_settings(ctx: Ctx = Depends(get_ctx)):
    """«Настройки → Табель»: границы времени, задержка, штраф. Менять — директор, зам, админ; бухгалтер видит."""
    ctx.need("attendance.decide", "attendance.alerts")
    return ctx.render("attendance/settings.html", title="Табель — настройки", p=late_svc.params(ctx.db, ctx.branch.id),
                      can_edit=ctx.can("attendance.decide"))


@router.post("/settings/timesheet")
def ts_settings_save(ctx: Ctx = Depends(get_ctx), late_min: int = Form(15), absent_min: int = Form(60),
                     over_info_min: int = Form(30), over_red_min: int = Form(60), fine_amount: int = Form(500),
                     fine_from: int = Form(3)):
    ctx.need("attendance.decide")
    if not (0 <= late_min < absent_min <= 600 and 0 < over_info_min <= over_red_min <= 600 and fine_amount >= 0 and fine_from >= 1):
        flash(ctx.request, "Проверьте значения: опоздание < неявки, сведения ≤ «красной» задержки", "error")
        return RedirectResponse("/settings/timesheet", status_code=303)
    st.put(ctx.db, ctx.branch.id, "attendance", {"late_min": late_min, "absent_min": absent_min, "over_info_min": over_info_min,
                                                 "over_red_min": over_red_min, "fine_amount": fine_amount, "fine_from": fine_from})
    ctx.db.commit()
    flash(ctx.request, "Настройки табеля сохранены")
    return RedirectResponse("/settings/timesheet", status_code=303)


@router.get("/timesheet")
def timesheet(ctx: Ctx = Depends(get_ctx), m: str = "", user: str = "", p: str = "", d: str = ""):
    ctx.need("attendance.view_all", "attendance.view_own")
    import calendar
    from ..services import periods
    if d and not m:
        m = d[:7]   # общая панель дат: табель всегда по месяцам
    y, mo = _month(m)
    per = periods.parse("month", date(y, mo, 1).isoformat(), allowed=("month",))
    bar = periods.Bar(per, "/timesheet", {"user": user if ctx.can("attendance.view_all") else ""})
    n_days = calendar.monthrange(y, mo)[1]
    prev = (date(y, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    nxt = (date(y, mo, n_days) + timedelta(days=1)).strftime("%Y-%m")
    see_all = ctx.can("attendance.view_all")
    staff = staff_for_period(ctx.db, ctx.branch.id, date(y, mo, 1))  # уволенные в этом месяце — тоже
    target = None
    if not see_all:
        target = ctx.user
    elif user:
        target = ctx.db.get(User, _uuid(user))
    if target:
        grid = svc.month_grid(ctx.db, ctx.branch.id, [target], y, mo)[target.id]
        lt = _alerts(ctx, only=target)
        late_days = {f.day.day for f in late_svc.flags(ctx.db, ctx.branch.id, [target]) if f.kind == "late"
                     and f.day.year == y and f.day.month == mo} if target.role.code != "admin" else set()
        # 24.11.2026: по каждому дню — что было (опоздание, неявка, ранний уход, задержка), решение, «отпросился»
        from ..models import AttendanceLeave
        dec = late_svc.decisions(ctx.db, ctx.branch.id, date(y, mo, 1))
        day_flags = {}
        if target.role.code != "admin":
            for f in late_svc.flags(ctx.db, ctx.branch.id, [target], back_days=62):
                if f.day.year == y and f.day.month == mo:
                    day_flags.setdefault(f.day.day, []).append({"f": f, "d": dec.get((target.id, f.day, f.kind))})
        lv_days = {}
        for lv in ctx.db.scalars(select(AttendanceLeave).where(AttendanceLeave.user_id == target.id,
                                                               AttendanceLeave.day >= date(y, mo, 1),
                                                               AttendanceLeave.day <= date(y, mo, n_days))):
            lv_days.setdefault(lv.day.day, []).append(lv)
        people_ = {x.id: x for x in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
        return ctx.render("attendance/user.html", title="Табель", u=target, grid=grid, y=y, mo=mo, n_days=n_days,
                          month_name=MONTHS[mo], prev=prev, nxt=nxt, see_all=see_all, date=date, bar=bar,
                          total=sum(d.minutes for d in grid.values()), can_edit=ctx.can("attendance.edit"),
                          can_delete=ctx.user.role.code == "admin", late_days=late_days, day_flags=day_flags,
                          lv_days=lv_days, people_=people_, **lt,
                          local=lambda e: svc._aware(e.at).astimezone(svc.local_tz()))
    grid = svc.month_grid(ctx.db, ctx.branch.id, staff, y, mo)
    now_on = svc.on_site_now(ctx.db, ctx.branch.id, staff)
    weekends = {d for d in range(1, n_days + 1) if date(y, mo, d).weekday() >= 5}
    lt = _alerts(ctx)
    # 24.11.2026 (брат, вечер): значки в клетках — опоздание / неявка / ранний уход / задержка, решение; «отпросился»
    from ..models import AttendanceLeave
    marks = {}
    dec = late_svc.decisions(ctx.db, ctx.branch.id, date(y, mo, 1))
    for f in late_svc.flags(ctx.db, ctx.branch.id, [u for u in staff if u.active], back_days=62):
        if f.day.year == y and f.day.month == mo and f.kind != "no_out":
            x = dec.get((f.user.id, f.day, f.kind))
            marks.setdefault(f.user.id, {}).setdefault(f.day.day, []).append({"k": f.kind, "d": x.decision if x else ""})
    lvs = {}
    for lv in ctx.db.scalars(select(AttendanceLeave).where(AttendanceLeave.branch_id == ctx.branch.id,
                                                           AttendanceLeave.day >= date(y, mo, 1),
                                                           AttendanceLeave.day <= date(y, mo, n_days))):
        lvs.setdefault(lv.user_id, {}).setdefault(lv.day.day, []).append(lv)
    return ctx.render("attendance/month.html", title="Табель", staff=staff, grid=grid, now_on=now_on, y=y, mo=mo,
                      n_days=n_days, month_name=MONTHS[mo], prev=prev, nxt=nxt, weekends=weekends, bar=bar, **lt,
                      marks=marks, lvs=lvs,
                      today=date.today(), local=lambda e: svc._aware(e.at).astimezone(svc.local_tz()))


@router.post("/timesheet/events")
def event_add(ctx: Ctx = Depends(get_ctx), user_id: str = Form(...), day: str = Form(...), at: str = Form(...),
              direction: str = Form("in"), comment: str = Form("")):
    """Ручная отметка (забыл карту, не отметил уход) — с комментарием. 24.11.2026 (брат): вписывают бухгалтер,
    директор, зам и админ (право attendance.edit; раньше только главный админ)."""
    ctx.need("attendance.edit")
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
