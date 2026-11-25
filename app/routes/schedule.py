"""Запись клиентов на будущее (пункт 2): день — сетка по мастерам, неделя — список по дням."""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import Appointment, Client, Lift, User, Vehicle, WorkOrder
from ..models.schedule import APPT_STATUS
from ..services import orders as osvc
from ..services import schedule as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid
from ..permissions import MASTERS

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


def _level(p: int) -> str:
    """Цвет загрузки, как в «Загрузке мастеров»: до 60 % зелёный, 60–90 % оранжевый, больше 90 % красный."""
    return "red" if p > 90 else "orange" if p >= 60 else "green"


def _wo_items(segs: list) -> list:
    """Куски нарядов мастера за день → по одной карточке на наряд (с первого куска до последнего)."""
    acc = {}
    for s, e, b in segs:
        x = acc.setdefault(id(b), {"kind": "wo", "b": b, "s": s, "e": e, "mins": 0})
        x["s"], x["e"] = min(x["s"], s), max(x["e"], e)
        x["mins"] += int((e - s).total_seconds() // 60)
    return [dict(x, t=x["s"].strftime("%H:%M"), end=x["e"].strftime("%H:%M")) for x in acc.values()]


def _appt_item(a) -> dict:
    s = svc.local(a.start_at)
    return {"kind": "appt", "a": a, "t": s.strftime("%H:%M"),
            "end": (s + timedelta(minutes=a.duration_min or 30)).strftime("%H:%M"), "mech": a.mechanic_id}


def _hourly(free: list) -> list:
    """Свободное окно → по часу (22.11.2026, брат): «08:00–09:00», «09:00–10:00»… — на каждый час своя «＋ Записать».
    Неполный кусок в начале или в конце окна (например 15:30–16:00) — если в нём не меньше 30 минут."""
    out = []
    for a, b, _ in free:
        s_, e_ = _mins(a), _mins(b)
        cur = s_
        while cur < e_:
            nxt = min(e_, (cur // 60 + 1) * 60)
            if nxt - cur >= 30:
                out.append((_hhmm(cur), _hhmm(nxt), nxt - cur))
            cur = nxt
    return out


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _sorted(items: list) -> list:
    """По времени. У «свободно», на которое легли наряды «возможно» (согласование, диагностика), — пометка: окно
    свободно, но если клиент согласует, мастер будет занят (в свободные окна «возможно» не входит — решение 22.11)."""
    order = {"appt": 0, "wo": 1, "free": 2}
    tent = [(x["t"], x["end"], x.get("mech")) for x in items if x["kind"] == "wo" and x["b"].kind != "solid"]
    for x in items:
        if x["kind"] == "free":
            x["tent"] = any(s < x["end"] and x["t"] < e and (m is None or x.get("mech") in (None, m)) for s, e, m in tent)
    return sorted(items, key=lambda x: (x["t"], order[x["kind"]]))


@router.get("/schedule")
def schedule(ctx: Ctx = Depends(get_ctx), d: str = "", view: str = "day", edit: str = "", p: str = "", by: str = "",
             f: str = "", t: str = "", show: str = "", m: str = "", fd: str = "", free: str = "", mv: str = ""):
    """Запись (22.11.2026, брат — новый вид «столбиками»). День — столбик на каждого мастера; неделя — столбик на
    каждый день; месяц — календарь. В столбике по времени: записи клиентов, наряды мастера и «свободно» (+ записать).
    Наверху плитки (записей / ждём / приехали / не приехали), кнопки «Новая запись», «Свободные окна», «Напомнить о
    записи», «Загрузка мастеров»; выбор мастера (?m=). Мастер видит только себя, без кнопок записи."""
    ctx.need("schedule.view", "schedule.manage")
    if show in ("future", "past"):
        return _lists(ctx, show, by)
    return _board(ctx, d, view, edit, p, f, t, m, fd, free, mv)


def _mark_queue(items: list, wl: dict, mid) -> list:
    """Наряды в столбике мастера: где стоят в его очереди (первый / последний своего вида) — для стрелок ↑ ↓."""
    from ..services import workload as wl_svc
    q = wl_svc.queue_of(wl, mid)
    for x in items:
        if x["kind"] == "wo":
            same = [b.wo.id for b in q if b.kind == x["b"].kind]
            i = same.index(x["b"].wo.id) if x["b"].wo.id in same else -1
            x.update(mech=mid, q_first=i <= 0, q_last=i == len(same) - 1 or i < 0)
    return items


def _board(ctx: Ctx, d, view, edit, p, f, t, m, fd, free, mv=""):
    from ..services import periods
    from ..services import workload as wl_svc
    db, bid = ctx.db, ctx.branch.id
    today = date.today()
    kind = p if p in ("day", "week", "month", "custom") else (view if view in ("week", "month") else "day")
    per = periods.parse(kind, d, f, t, default="day", allowed=("day", "week", "month", "custom"))
    view, day = per.kind, per.anchor
    all_masters = osvc.staff_by_role(db, bid, "masters")
    colors = svc.mech_colors(all_masters)   # цвет мастера один и тот же у всех
    can_manage = ctx.can("schedule.manage")
    own = not can_manage
    masters = [x for x in all_masters if x.id == ctx.user.id] if own else all_masters
    pick = None if own else next((x for x in masters if str(x.id) == m), None)
    # 22.11.2026 (брат): неделя и месяц — только по одному мастеру (с «Все мастера» путались); не выбран — первый
    if view != "day" and not own and not pick and masters:
        pick = masters[0]
    shown = [pick] if pick else masters
    ids = {x.id for x in shown}
    lifts = db.scalars(select(Lift).where(Lift.branch_id == bid, Lift.active.is_(True)).order_by(Lift.number)).all()
    own_lift = {l.mechanic_id: l for l in lifts if l.mechanic_id}
    keep = {"m": str(pick.id) if pick else ""}
    bar = periods.Bar(per, "/schedule", keep)
    editing = db.get(Appointment, _uuid(edit)) if edit else None
    if editing and (editing.branch_id != bid or (own and editing.mechanic_id != ctx.user.id)):
        editing = None
    wl = wl_svc.plan(db, bid, all_masters)
    # Период: записи выбранных мастеров (у руководителя без выбора — и «без мастера»)
    d1, d2 = (day, day) if view == "day" else (per.d1, per.d2)
    if (d2 - d1).days > 92:   # свой период — не больше квартала
        d2 = d1 + timedelta(days=92)
    # записи «без мастера» руководитель видит всегда (и в неделе / месяце одного мастера — с подписью «Без мастера»)
    appts = [a for a in svc.between(db, bid, d1, d2) if a.mechanic_id in ids or (not own and not a.mechanic_id
                                                                                  and (not pick or view != "day"))]
    cfg_load = svc.period_load(db, bid, d1, d2, shown, appts,
                               extra={x.id: wl_svc.minutes_by_day(wl, x.id) for x in shown})
    day_h, days_off = cfg_load["_day_h"], cfg_load["_off"]
    # Свободные окна: на выбранный день (в «Дне» — этот день, иначе сегодня или первый день периода)
    fday = _day(fd) if fd else (day if view == "day" else max(today, d1))
    def free_cols(dd, who):
        cols = [{"id": x.id} for x in who]
        aa = svc.between(db, bid, dd, dd)
        segs = wl_svc.segments_on(wl, dd)
        return svc.day_load(db, bid, dd, cols, aa, min_gap=30,
                            extra={k: [x for x in v if x[2].kind == "solid"] for k, v in segs.items()})
    fl = free_cols(fday, shown) if can_manage else {}
    free_rows = [{"m": x, "free": _hourly(fl.get(x.id, {}).get("free", []))} for x in shown] if can_manage else []
    base = dict(view=view, day=day, per=per, bar=bar, today=today, masters=masters, shown=shown, pick=pick, own=own,
                all_masters=all_masters, colors=colors, own_lift=own_lift, lifts=lifts, editing=editing,
                can_manage=can_manage, APPT_STATUS=APPT_STATUS, fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate,
                local=svc.local, WEEKDAYS=WEEKDAYS, MON=MON, summary=_summary(appts), day_h=day_h, days_off=days_off,
                remind=svc.reminders(db, bid) if can_manage else None, fday=fday, free_rows=free_rows,
                open_free=bool(free) and can_manage, level=_level,
                load_ok=ctx.can("dashboard.view"), people={x.id: x for x in all_masters},
                new_day=day if view == "day" and day >= today else max(today, d1),
                can_queue=ctx.can("schedule.queue"), moving=bool(mv) and ctx.can("schedule.manage"),
                wo_off=[b for b in wl["off"] if b.mech_id in ids])
    # 21.11.2026 (брат): у мастера плитки — сигналы «что нового у меня» (🟣 новые, 🟠 ждём, 🟢 приехали, 🔴 не приехали)
    sig = svc.master_signals(db, bid, ctx.user.id) if own and ctx.user.role and ctx.user.role.code in MASTERS else None
    base.update(sig=sig, new_ids={a.id for a in sig["new"] + sig["noshow"]} if sig else set(),
                sig_orders={a.work_order_id: db.get(WorkOrder, a.work_order_id) for a in sig["arrived"]} if sig else {})
    if view == "day":
        segs = wl_svc.segments_on(wl, day)
        load = svc.day_load(db, bid, day, [{"id": x.id} for x in shown], appts, min_gap=30,
                            extra={k: [x for x in v if x[2].kind == "solid"] for k, v in segs.items()})
        work_min = max(1, day_h * 60)
        cols = []
        for x in shown:
            ld = load[x.id]
            items = [_appt_item(a) for a in appts if a.mechanic_id == x.id]
            items += _mark_queue(_wo_items(segs.get(x.id, [])), wl, x.id)
            if day.weekday() not in days_off:   # в выходной свободные часы не предлагаем — только «＋ Записать всё равно»
                items += [{"kind": "free", "t": a, "end": b, "mins": mm, "mech": x.id} for a, b, mm in _hourly(ld["free"])]
            pct = min(100, round(ld["booked"] * 100 / work_min))
            cols.append({"m": x, "items": _sorted(items), "pct": pct, "lv": _level(pct), "booked": ld["booked"] / 60,
                         "n": sum(1 for a in appts if a.mechanic_id == x.id and a.status != "cancel"),
                         "off": day.weekday() in days_off})
        loose = [_appt_item(a) for a in appts if not a.mechanic_id]
        if loose:
            cols.append({"m": None, "items": _sorted(loose), "pct": 0, "lv": "green", "booked": 0, "n": len(loose), "off": False})
        return ctx.render("schedule/index.html", title="Запись", cols=cols, **base)
    days = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    by_day = {x: [] for x in days}
    for a in appts:
        by_day.setdefault(svc.local(a.start_at).date(), []).append(a)
    cap = max(1, len(shown)) * day_h * 60
    mins_day = {dd: sum(cfg_load[x.id]["by_day"].get(dd, 0) for x in shown) for dd in days}
    pct_day = {dd: min(100, round(mins_day[dd] * 100 / cap)) for dd in days}
    if view == "week":
        cols = []
        for dd in days:
            segs = wl_svc.segments_on(wl, dd)
            items = [_appt_item(a) for a in by_day.get(dd, [])]
            for x in shown:
                items += _mark_queue(_wo_items(segs.get(x.id, [])), wl, x.id)
            if pick and can_manage and dd >= today and dd.weekday() not in days_off:
                fr = free_cols(dd, [pick]).get(pick.id, {}).get("free", [])
                items += [{"kind": "free", "t": a, "end": b, "mins": mm, "mech": pick.id} for a, b, mm in _hourly(fr)]
            srt = _sorted(items)
            cols.append({"d": dd, "items": srt, "pct": pct_day[dd], "lv": _level(pct_day[dd]),
                         "first": next((x["t"] for x in srt if x["kind"] == "free"), ""),
                         "hours": mins_day[dd] / 60, "n": len([a for a in by_day.get(dd, []) if a.status != "cancel"]),
                         "off": dd.weekday() in days_off})
        return ctx.render("schedule/index.html", title="Запись", cols=cols, **base)
    # Месяц / свой период — календарь: недели с понедельника, дни вне периода пустые
    weeks, s = [], d1 - timedelta(days=d1.weekday())
    while s <= d2:
        weeks.append([s + timedelta(days=i) for i in range(7)])
        s += timedelta(days=7)
    wo_n = {dd: len({id(b) for x in shown for _, _, b in wl_svc.segments_on(wl, dd).get(x.id, [])}) for dd in days}
    # 22.11.2026 (брат): месяц по мастеру — у каждого дня «свободно N ч» и первый свободный час для «＋ Записать»
    free_day = {}
    for dd in days:
        if pick and can_manage and dd >= today and dd.weekday() not in days_off:
            hrs = _hourly(free_cols(dd, [pick]).get(pick.id, {}).get("free", []))
            free_day[dd] = {"mins": sum(x[2] for x in hrs), "first": hrs[0][0] if hrs else ""}
    return ctx.render("schedule/index.html", title="Запись", weeks=weeks, by_day=by_day, d1=d1, d2=d2,
                      pct_day=pct_day, mins_day=mins_day, wo_n=wo_n, free_day=free_day, **base)


MON = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


@router.post("/schedule/queue/{wid}")
def queue_move(wid: str, ctx: Ctx = Depends(get_ctx), mech: str = Form(...), where: str = Form("up"), back: str = Form("")):
    """Очередь нарядов мастера (22.11.2026, брат): ↑ — раньше, ↓ — позже (среди нарядов того же вида: сплошные /
    «возможно»). Записываем место всей очереди мастера — дальше порядок держится, новые наряды встают в конец."""
    ctx.need("schedule.queue")
    from ..models import MasterQueue
    from ..services import workload as wl_svc
    db, bid = ctx.db, ctx.branch.id
    wo_id, mid = _uuid(wid), _uuid(mech)
    masters = osvc.staff_by_role(db, bid, "masters")
    if mid not in {x.id for x in masters}:
        raise Forbidden()
    q = wl_svc.queue_of(wl_svc.plan(db, bid, masters), mid)
    blk = next((b for b in q if b.wo.id == wo_id), None)
    if blk:
        same = [b for b in q if b.kind == blk.kind]
        i = same.index(blk)
        j = i - 1 if where == "up" else i + 1
        if 0 <= j < len(same):
            same[i], same[j] = same[j], same[i]
            order = [b.wo.id for b in (same if blk.kind == "solid" else [b for b in q if b.kind == "solid"])]
            order += [b.wo.id for b in ([b for b in q if b.kind != "solid"] if blk.kind == "solid" else same)]
            rows = {r.work_order_id: r for r in db.scalars(select(MasterQueue).where(
                MasterQueue.branch_id == bid, MasterQueue.mechanic_id == mid))}
            for n, oid in enumerate(order, 1):
                r = rows.get(oid)
                if r is None:
                    db.add(MasterQueue(mechanic_id=mid, work_order_id=oid, rank=n))
                elif r.rank != n:
                    r.rank = n
            db.commit()
            other = same[i]   # с кем поменялись местами
            flash(ctx.request, f"Наряд {blk.wo.number[-6:]} — {'раньше' if where == 'up' else 'позже'} "
                               f"наряда {other.wo.number[-6:]}")
    return RedirectResponse(_back(back, "/schedule"), status_code=303)


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
    mechanics = osvc.staff_by_role(db, bid, "masters")
    groups, cur = [], None
    for a in appts:
        k = svc.local(a.start_at).date()
        if k != cur:
            groups.append({"d": k, "items": []})
            cur = k
        groups[-1]["items"].append(_appt_item(a))
    return ctx.render("schedule/list.html", title="Все записи списком", show=show, groups=groups,
                      people={u.id: u for u in mechanics}, colors=svc.mech_colors(mechanics), today=today,
                      APPT_STATUS=APPT_STATUS, fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate,
                      WEEKDAYS=WEEKDAYS, MON=MON, summary=_summary(appts), can_manage=ctx.can("schedule.manage"))


def _fill(ctx: Ctx, a: Appointment, f: dict):
    bid = ctx.branch.id
    a.start_at = svc.to_utc(_day(f["day"]), f["time"])
    try:
        a.duration_min = max(15, min(int(f["duration"] or 60), 12 * 60))
    except ValueError:
        a.duration_min = 60
    m = ctx.db.get(User, uuid.UUID(f["mechanic_id"])) if f["mechanic_id"] else None
    a.mechanic_id = m.id if m and m.branch_id == bid and m.role.code in MASTERS else None
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
        from ..services import clients_card as _cc   # 25.11.2026: любой номер карточки
        c = _cc.by_phone(ctx.db, bid, a.phone)
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


def _back(back: str, default: str) -> str:
    """Вернуться туда, откуда записывали (тот же день / неделя / мастер), — только внутри «Записи»."""
    return back if back.startswith("/schedule") and not back.startswith("//") else default


_FIELDS = ("day", "time", "duration", "mechanic_id", "lift_id", "client_name", "phone", "car", "plate", "work", "note")


@router.post("/schedule")
def appt_add(ctx: Ctx = Depends(get_ctx), day: str = Form(""), time: str = Form("09:00"), duration: str = Form("60"),
             mechanic_id: str = Form(""), lift_id: str = Form(""), client_name: str = Form(""), phone: str = Form(""),
             car: str = Form(""), plate: str = Form(""), work: str = Form(""), note: str = Form(""), view: str = Form("day"),
             back: str = Form("")):
    ctx.need("schedule.manage")
    f = dict(zip(_FIELDS, (day, time, duration, mechanic_id, lift_id, client_name, phone, car, plate, work, note)))
    if not (client_name.strip() or phone.strip()):
        flash(ctx.request, "Укажите имя или телефон клиента", "error")
        return RedirectResponse(_back(back, f"/schedule?d={day}&view={view}"), status_code=303)
    a = Appointment()
    _fill(ctx, a, f)
    ctx.db.add(a)
    ctx.db.flush()
    _warn_overlap(ctx, a)
    ctx.db.commit()
    return RedirectResponse(_back(back, f"/schedule?d={_day(day)}&view={view}"), status_code=303)


def _appt(ctx: Ctx, aid: str) -> Appointment:
    a = ctx.db.get(Appointment, _uuid(aid))
    if not a or a.branch_id != ctx.branch.id:
        raise Forbidden()
    return a


@router.post("/schedule/{aid}")
def appt_edit(aid: str, ctx: Ctx = Depends(get_ctx), day: str = Form(""), time: str = Form("09:00"), duration: str = Form("60"),
              mechanic_id: str = Form(""), lift_id: str = Form(""), client_name: str = Form(""), phone: str = Form(""),
              car: str = Form(""), plate: str = Form(""), work: str = Form(""), note: str = Form(""), view: str = Form("day"),
             back: str = Form("")):
    ctx.need("schedule.manage")
    a = _appt(ctx, aid)
    _fill(ctx, a, dict(zip(_FIELDS, (day, time, duration, mechanic_id, lift_id, client_name, phone, car, plate, work, note))))
    ctx.db.flush()
    _warn_overlap(ctx, a)
    ctx.db.commit()
    return RedirectResponse(_back(back, f"/schedule?d={_day(day)}&view={view}"), status_code=303)


@router.post("/schedule/{aid}/seen")
def appt_seen(aid: str, ctx: Ctx = Depends(get_ctx), back: str = Form("")):
    """21.11.2026 (брат): мастер «✓ Видел» — новая запись к нему / «не приехал». aid = "all" — все сразу.
    Отмечает только сам мастер записи (и админ «глазами сотрудника» — от его имени)."""
    ctx.need("schedule.view")
    if aid == "all":
        sig = svc.master_signals(ctx.db, ctx.branch.id, ctx.user.id)
        todo = sig["new"] + sig["noshow"]
    else:
        a = _appt(ctx, aid)
        if a.mechanic_id != ctx.user.id:
            raise Forbidden()
        todo = [a]
    for a in todo:
        svc.mark_seen(a)
    ctx.db.commit()
    if todo:
        flash(ctx.request, "Отмечено: видел" + (f" ({len(todo)})" if len(todo) > 1 else ""))
    return RedirectResponse(_back(back, "/schedule"), status_code=303)


@router.post("/schedule/{aid}/status")
def appt_status(aid: str, ctx: Ctx = Depends(get_ctx), status: str = Form(...), view: str = Form("day"),
                back: str = Form("")):
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
    return RedirectResponse(_back(back, f"/schedule?d={d}&view={view}"), status_code=303)


@router.get("/schedule/remind")
def remind(ctx: Ctx = Depends(get_ctx)):
    """«Напомнить о записи» (20.11.2026, брат): 🔴 сегодня не приехали (время прошло), 🟠 завтра — позвонить сегодня.
    «✓ Позвонил — подтвердил» или «Отменить запись»."""
    ctx.need("schedule.manage")
    r = svc.reminders(ctx.db, ctx.branch.id)
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("schedule/remind.html", title="Напомнить о записи", r=r, people=people, local=svc.local,
                      fmt_phone=osvc.fmt_phone, fmt_plate=osvc.fmt_plate, today=date.today())


@router.post("/schedule/{aid}/called")
def appt_called(aid: str, ctx: Ctx = Depends(get_ctx)):
    """Позвонил — клиент подтвердил, что приедет (запись остаётся «Записан», из напоминаний уходит)."""
    ctx.need("schedule.manage")
    from ..db import now
    a = _appt(ctx, aid)
    a.called_at, a.called_by = now(), ctx.user.id
    ctx.db.commit()
    flash(ctx.request, f"Позвонили: {a.client_name or a.phone} — подтвердил")
    return RedirectResponse("/schedule/remind", status_code=303)


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
