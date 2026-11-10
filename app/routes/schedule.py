"""Запись клиентов на будущее (пункт 2): день — сетка по мастерам, неделя — список по дням."""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import Appointment, Client, Lift, User, Vehicle
from ..models.schedule import APPT_STATUS
from ..services import orders as osvc
from ..services import schedule as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _day(v: str) -> date:
    try:
        return date.fromisoformat(v) if v else date.today()
    except ValueError:
        return date.today()


def _own_only(ctx: Ctx) -> bool:
    """Слесарь без права записывать видит только свои записи."""
    return not ctx.can("schedule.manage")


BY = {"mech": "По мастерам", "date": "По датам"}


@router.get("/schedule")
def schedule(ctx: Ctx = Depends(get_ctx), d: str = "", view: str = "day", edit: str = "", p: str = "", by: str = "",
             f: str = "", t: str = "", show: str = ""):
    """Запись (11.11.2026): день / неделя / месяц / свой период; «по мастерам» или «по датам»; загрузка мастеров;
    списки «будущие» и «прошлые». Старые ссылки ?view=week продолжают работать."""
    ctx.need("schedule.view", "schedule.manage")
    from ..services import periods
    db, bid = ctx.db, ctx.branch.id
    if show in ("future", "past"):
        return _lists(ctx, show, by)
    kind = p if p in ("day", "week", "month", "custom") else (view if view in ("week", "month") else "day")
    per = periods.parse(kind, d, f, t, default="day", allowed=("day", "week", "month", "custom"))
    view = per.kind
    day = per.anchor
    by = by if by in BY else ("mech" if view in ("day", "month") else "date")
    bar = periods.Bar(per, "/schedule", {"by": by})
    mechanics = osvc.staff_by_role(db, bid, "mechanic")
    if _own_only(ctx):
        mechanics = [m for m in mechanics if m.id == ctx.user.id]
    lifts = db.scalars(select(Lift).where(Lift.branch_id == bid, Lift.active.is_(True)).order_by(Lift.number)).all()
    mine = ctx.user.id if _own_only(ctx) else None
    editing = db.get(Appointment, _uuid(edit)) if edit else None
    if editing and editing.branch_id != bid:
        editing = None
    colors = svc.mech_colors(osvc.staff_by_role(db, bid, "mechanic"))  # цвет мастера один и тот же у всех
    own_lift = {l.mechanic_id: l for l in lifts if l.mechanic_id}
    base = dict(day=day, view=view, mechanics=mechanics, lifts=lifts, editing=editing, APPT_STATUS=APPT_STATUS,
                fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate, local=svc.local, WEEKDAYS=WEEKDAYS,
                can_manage=ctx.can("schedule.manage"), today=date.today(), colors=colors, own_lift=own_lift, bar=bar,
                per=per, by=by, BY=BY, show="")
    if view in ("week", "month", "custom"):
        d1, d2 = per.d1, per.d2
        if (d2 - d1).days > 92:   # свой период — не больше квартала, чтобы страница не тормозила
            d2 = d1 + timedelta(days=92)
        days = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
        appts = svc.between(db, bid, d1, d2, mechanic_id=mine, with_cancelled=False)
        by_day = {x: [] for x in days}
        for a in appts:
            by_day.setdefault(svc.local(a.start_at).date(), []).append(a)
        people = {u.id: u for u in mechanics}
        hours = {d: sum((a.duration_min or 0) for a in by_day.get(d, []) if a.status in ("booked", "arrived")) / 60 for d in days}
        pl = svc.period_load(db, bid, d1, d2, mechanics, appts)
        cap = max(1, len(mechanics)) * pl["_day_h"]   # часов работы всех мастеров в день
        # Календарь месяца: недели по 7 дней, с понедельника; дни чужого месяца — пустые
        weeks = []
        if view == "month":
            s = d1 - timedelta(days=d1.weekday())
            while s <= d2:
                weeks.append([s + timedelta(days=i) for i in range(7)])
                s += timedelta(days=7)
        no_mech = [a for a in appts if not a.mechanic_id]
        return ctx.render("schedule/index.html", title="Запись", days=days, by_day=by_day, people=people, hours=hours,
                          load={d: min(100, round(hours[d] * 100 / cap)) for d in days}, pl=pl, weeks=weeks,
                          appts=appts, no_mech=no_mech, day_h=pl["_day_h"], days_off=pl["_off"],
                          summary=_summary(appts), **base)
    appts = svc.between(db, bid, day, day, mechanic_id=mine)
    columns = [{"id": m.id, "title": m.short_name, "user": m} for m in mechanics]
    if not mine and (any(a.mechanic_id is None for a in appts) or not columns):
        columns.append({"id": None, "title": "Без мастера", "user": None})  # только если есть такие записи
    g = svc.grid(db, bid, day, columns, appts)
    load = svc.day_load(db, bid, day, columns, appts)
    summary = _summary(appts)
    tomorrow = []
    if ctx.can("schedule.manage"):
        tomorrow = [a for a in svc.between(db, bid, date.today() + timedelta(days=1), date.today() + timedelta(days=1))
                    if a.status == "booked"]
    return ctx.render("schedule/index.html", title="Запись", columns=columns, g=g, appts=appts, load=load,
                      summary=summary, tomorrow=tomorrow, people={m.id: m for m in mechanics}, **base)


def _summary(appts) -> dict:
    return {"all": len(appts), "arrived": sum(a.status == "arrived" for a in appts),
            "noshow": sum(a.status == "noshow" for a in appts), "booked": sum(a.status == "booked" for a in appts)}


def _lists(ctx: Ctx, show: str, by: str):
    """Будущие (от сегодня, 60 дней) или прошлые (90 дней назад) записи — списком, по датам или по мастерам."""
    db, bid = ctx.db, ctx.branch.id
    today = date.today()
    d1, d2 = (today, today + timedelta(days=60)) if show == "future" else (today - timedelta(days=90), today - timedelta(days=1))
    mine = ctx.user.id if _own_only(ctx) else None
    appts = svc.between(db, bid, d1, d2, mechanic_id=mine, with_cancelled=True)
    if show == "past":
        appts = list(reversed(appts))
    mechanics = osvc.staff_by_role(db, bid, "mechanic")
    people = {u.id: u for u in mechanics}
    by = by if by in BY else "date"
    groups = []
    if by == "mech":
        for m in mechanics + [None]:
            rows = [a for a in appts if a.mechanic_id == (m.id if m else None)]
            if rows:
                groups.append({"title": m.full_name if m else "Без мастера", "mech": m.id if m else None, "rows": rows})
    else:
        cur = None
        for a in appts:
            k = svc.local(a.start_at).date()
            if k != cur:
                groups.append({"title": k, "rows": []})
                cur = k
            groups[-1]["rows"].append(a)
    from ..services import periods
    per = periods.parse("day", today.isoformat(), allowed=("day", "week", "month", "custom"))
    return ctx.render("schedule/index.html", title="Запись", show=show, groups=groups, people=people, appts=appts,
                      by=by, BY=BY, view="list", day=today, today=today, mechanics=mechanics, lifts=[], editing=None,
                      APPT_STATUS=APPT_STATUS, fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate, local=svc.local,
                      WEEKDAYS=WEEKDAYS, can_manage=False, colors=svc.mech_colors(mechanics), own_lift={},
                      bar=periods.Bar(per, "/schedule", {"by": by}), per=per, summary=_summary(appts),
                      list_range=(d1, d2))


def _fill(ctx: Ctx, a: Appointment, f: dict):
    bid = ctx.branch.id
    a.start_at = svc.to_utc(_day(f["day"]), f["time"])
    try:
        a.duration_min = max(15, min(int(f["duration"] or 60), 12 * 60))
    except ValueError:
        a.duration_min = 60
    m = ctx.db.get(User, uuid.UUID(f["mechanic_id"])) if f["mechanic_id"] else None
    a.mechanic_id = m.id if m and m.branch_id == bid and m.role.code == "mechanic" else None
    lift = ctx.db.get(Lift, uuid.UUID(f["lift_id"])) if f["lift_id"] else None
    a.lift_id = lift.id if lift and lift.branch_id == bid else None
    if not a.lift_id and a.mechanic_id:   # пост мастера по умолчанию
        own = ctx.db.scalar(select(Lift).where(Lift.branch_id == bid, Lift.mechanic_id == a.mechanic_id))
        a.lift_id = own.id if own else None
    a.client_name = f["client_name"].strip()[:160]
    a.phone = osvc.norm_phone(f["phone"])
    a.car = f["car"].strip()[:120]
    a.plate = osvc.norm_plate(f["plate"])[:20]
    a.work = f["work"].strip()
    a.note = f["note"].strip()
    # Знакомый клиент — привязываем по телефону / номеру
    if a.phone and not a.client_id:
        c = ctx.db.scalar(select(Client).where(Client.branch_id == bid, Client.phone == a.phone))
        a.client_id = c.id if c else None
    if a.plate and not a.vehicle_id:
        v = ctx.db.scalar(select(Vehicle).where(Vehicle.branch_id == bid, Vehicle.plate == a.plate))
        if v:
            a.vehicle_id = v.id
            a.client_id = a.client_id or v.client_id


def _warn_overlap(ctx: Ctx, a: Appointment):
    clash = svc.overlaps(ctx.db, a)
    if clash:
        b = clash[0]
        flash(ctx.request, f"Записано, но есть пересечение: {svc.local(b.start_at).strftime('%H:%M')} "
                           f"{b.client_name or b.car} — у того же мастера/поста", "error")
    else:
        flash(ctx.request, f"Записан на {svc.local(a.start_at).strftime('%d.%m %H:%M')}")


_FIELDS = ("day", "time", "duration", "mechanic_id", "lift_id", "client_name", "phone", "car", "plate", "work", "note")


@router.post("/schedule")
def appt_add(ctx: Ctx = Depends(get_ctx), day: str = Form(""), time: str = Form("09:00"), duration: str = Form("60"),
             mechanic_id: str = Form(""), lift_id: str = Form(""), client_name: str = Form(""), phone: str = Form(""),
             car: str = Form(""), plate: str = Form(""), work: str = Form(""), note: str = Form(""), view: str = Form("day")):
    ctx.need("schedule.manage")
    f = dict(zip(_FIELDS, (day, time, duration, mechanic_id, lift_id, client_name, phone, car, plate, work, note)))
    if not (client_name.strip() or phone.strip()):
        flash(ctx.request, "Укажите имя или телефон клиента", "error")
        return RedirectResponse(f"/schedule?d={day}&view={view}", status_code=303)
    a = Appointment()
    _fill(ctx, a, f)
    ctx.db.add(a)
    ctx.db.flush()
    _warn_overlap(ctx, a)
    ctx.db.commit()
    return RedirectResponse(f"/schedule?d={_day(day)}&view={view}", status_code=303)


def _appt(ctx: Ctx, aid: str) -> Appointment:
    a = ctx.db.get(Appointment, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id:
        raise Forbidden()
    return a


@router.post("/schedule/{aid}")
def appt_edit(aid: str, ctx: Ctx = Depends(get_ctx), day: str = Form(""), time: str = Form("09:00"), duration: str = Form("60"),
              mechanic_id: str = Form(""), lift_id: str = Form(""), client_name: str = Form(""), phone: str = Form(""),
              car: str = Form(""), plate: str = Form(""), work: str = Form(""), note: str = Form(""), view: str = Form("day")):
    ctx.need("schedule.manage")
    a = _appt(ctx, aid)
    _fill(ctx, a, dict(zip(_FIELDS, (day, time, duration, mechanic_id, lift_id, client_name, phone, car, plate, work, note))))
    ctx.db.flush()
    _warn_overlap(ctx, a)
    ctx.db.commit()
    return RedirectResponse(f"/schedule?d={_day(day)}&view={view}", status_code=303)


@router.post("/schedule/{aid}/status")
def appt_status(aid: str, ctx: Ctx = Depends(get_ctx), status: str = Form(...), view: str = Form("day")):
    """Приехал → сразу приёмка с заполненными полями; не приехал / отменён / вернуть «записан»."""
    ctx.need("schedule.manage")
    a = _appt(ctx, aid)
    d = svc.local(a.start_at).date()
    if status == "arrived":
        if a.work_order_id:
            return RedirectResponse(f"/orders/{a.work_order_id}", status_code=303)
        return RedirectResponse(f"/orders/new?appt={a.id}", status_code=303)
    if status in ("noshow", "cancel", "booked"):
        a.status = status
        ctx.db.commit()
        flash(ctx.request, f"Запись: {APPT_STATUS[status].lower()}")
    return RedirectResponse(f"/schedule?d={d}&view={view}", status_code=303)


def prefill_from(ctx: Ctx, appt_id: str) -> dict:
    """Для формы приёмки: что известно из записи."""
    try:
        a = ctx.db.get(Appointment, uuid.UUID(appt_id)) if appt_id else None
    except ValueError:
        a = None
    if not a or a.branch_id != ctx.branch.id:
        return {}
    car = (a.car or "").split(" ", 1)
    out = {"appt_id": str(a.id), "phone": osvc.fmt_phone(a.phone) if a.phone else "", "name": a.client_name,
           "plate": osvc.fmt_plate(a.plate), "make": car[0] if car else "", "model": car[1] if len(car) > 1 else "",
           "complaint": a.work, "mechanic_id": str(a.mechanic_id or ""), "lift_id": str(a.lift_id or "")}
    if a.vehicle_id:
        v = ctx.db.get(Vehicle, a.vehicle_id)
        if v:
            out.update(make=v.make, model=v.model, year=str(v.year or ""), vin=v.vin, plate=osvc.fmt_plate(v.plate))
    if a.client_id:
        c = ctx.db.get(Client, a.client_id)
        if c:
            out.update(name=c.full_name, phone=osvc.fmt_phone(c.phone))
    return {k: v for k, v in out.items() if v}


def link_order(ctx: Ctx, appt_id: str, wo):
    """Наряд создан из записи: запись → «приехал», ссылка на наряд."""
    try:
        a = ctx.db.get(Appointment, uuid.UUID(appt_id)) if appt_id else None
    except ValueError:
        return
    if a and a.branch_id == ctx.branch.id and not a.work_order_id:
        a.status, a.work_order_id = "arrived", wo.id
        a.client_id, a.vehicle_id = wo.client_id, wo.vehicle_id
