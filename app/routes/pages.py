"""Главная, разделы-заготовки (наполняются на следующих шагах), пульт, журнал."""
from datetime import date

from fastapi import APIRouter, Depends
from ..services.periods import Bar
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import AuditLog, Lift, User
from ..web import Ctx, get_ctx

router = APIRouter()

# Что появится в разделе и на каком шаге — для пустых экранов
SECTIONS = {
}


@router.get("/")
def home(ctx: Ctx = Depends(get_ctx)):
    # Каждый попадает в первый доступный ему раздел; директор и зам — сразу в свой кабинет
    if ctx.user.role.code in ("director", "deputy") and ctx.can("reports.view", "reports.finance"):
        return RedirectResponse("/reports", status_code=303)
    target = ctx.menu[0]["href"] if ctx.menu else "/more"
    return RedirectResponse(target, status_code=303)


def _section(path: str):
    title, icon, step, text, need = SECTIONS[path]

    def view(ctx: Ctx = Depends(get_ctx)):
        ctx.need(*need)
        return ctx.render("section.html", title=title, icon_name=icon, step=step, text=text)
    return view


for _p in SECTIONS:
    router.add_api_route(_p, _section(_p), methods=["GET"])


@router.get("/pult")
def pult(ctx: Ctx = Depends(get_ctx)):
    ctx.need("dashboard.view")
    from sqlalchemy import func
    from ..models import IssueRequest, Vehicle, WorkOrder
    from ..services.orders import fmt_plate, lifts_with_load
    db, bid = ctx.db, ctx.branch.id
    lifts = lifts_with_load(db, bid)
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([wo.vehicle_id for _, wo in lifts if wo])))}
    staff = db.scalars(select(User).where(User.branch_id == bid, User.active.is_(True)).order_by(User.full_name)).all()
    people = {u.id: u for u in staff}
    from ..services.orders import visible_orders_query
    vis = visible_orders_query(ctx).subquery()   # менеджер — только свои наряды (12.11.2026)
    counts = dict(db.execute(select(vis.c.status, func.count()).group_by(vis.c.status)).all())
    issue_new = db.scalar(select(func.count()).select_from(IssueRequest)
                          .where(IssueRequest.branch_id == bid, IssueRequest.status == "new"))
    # Чем занят каждый: активные наряды, где он назначен
    busy = {}
    from ..models.orders import WO_ACTIVE
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(WO_ACTIVE))):
        for uid in (wo.mechanic_id, wo.partsman_id):
            if uid:
                busy[uid] = busy.get(uid, 0) + 1
    from ..models import WoWorkSession
    paused = {s.work_order_id: s.pause_reason for s in db.scalars(select(WoWorkSession).where(
        WoWorkSession.branch_id == bid, WoWorkSession.kind == "pause", WoWorkSession.ended_at.is_(None)))}
    from ..services.attendance import _aware, local_tz, on_site_now
    presence = {uid: (e.direction, _aware(e.at).astimezone(local_tz()).strftime("%H:%M")) if e else None
                for uid, e in on_site_now(db, bid, staff).items()}
    from ..services.orders import work_progress
    prog = work_progress(db, [wo.id for _, wo in lifts if wo])
    # Пульт (10.11.2026): плитки с цифрами, запись на сегодня, цвета мастеров, деньги за сегодня (директору / админу)
    from datetime import date as _date
    from ..services import schedule as ssvc
    from ..services.orders import staff_by_role
    colors = ssvc.mech_colors(staff_by_role(db, bid, "mechanic"))
    appts = ssvc.between(db, bid, _date.today(), _date.today())
    on_site = sum(1 for v in presence.values() if v and v[0] == "in")
    money_today = None
    if ctx.can("money.view", "reports.finance") and ctx.user.role.code in ("admin", "director", "deputy", "manager"):
        from ..services.reports import money_for
        money_today = money_for(db, bid, _date.today(), _date.today(), light=True).revenue
    tiles = [
        ("На СТО", sum(counts.get(s, 0) for s in WO_ACTIVE), "/orders?tab=active", "blue", "машин"),
        ("Диагностика", counts.get("diag", 0), "/orders?status=diag", "purple", "у слесарей"),
        ("Согласование", counts.get("approve", 0), "/orders?status=approve", "yellow", "ответ клиента"),
        ("Запчасти", counts.get("picking", 0) + counts.get("waiting", 0), "/orders?status=waiting", "orange",
         f"подбор {counts.get('picking', 0)} · ждём {counts.get('waiting', 0)}"),
        ("Готовы", counts.get("ready", 0) + counts.get("paid", 0), "/orders?status=ready", "green", "ждут клиента"),
        ("Запись сегодня", len([a for a in appts if a.status != "cancel"]), "/schedule", "teal",
         f"ждём {len([a for a in appts if a.status == 'booked'])}"),
        ("На месте", on_site, "/timesheet", "indigo", f"из {len(staff)}"),
    ]
    return ctx.render("pult.html", title="Пульт", lifts=lifts, prog=prog, vehicles=vehicles, people=people, staff=staff, paused=paused,
                      presence=presence, tiles=tiles, appts=appts, colors=colors, money_today=money_today,
                      issue_new=issue_new, local=ssvc.local, today=_date.today(),
                      counts=counts, busy=busy, fmt_plate=fmt_plate)


@router.get("/more")
def more(ctx: Ctx = Depends(get_ctx)):
    return ctx.render("more.html", title="Ещё")




# ---------- Журнал действий ----------
def _audit_filters(user: str, section: str, action: str, period: str, f: str, t: str, limit: int, p: str = "", d: str = "",
                   via: str = ""):
    from ..services import audit_view as av
    return av.Filters(user=user, section=section, action=action, period=period if period in av.PERIODS else "week",
                      f=f, t=t, limit=max(av.PAGE, min(limit or av.PAGE, 3000)), p=p, d=d,
                      via="admin" if via == "admin" else "")


@router.get("/audit")
def audit_view(ctx: Ctx = Depends(get_ctx), user: str = "", section: str = "", action: str = "", period: str = "week",
               f: str = "", t: str = "", limit: int = 0, p: str = "", d: str = "", via: str = ""):
    """Журнал: слева сотрудники (сколько действий), справа лента карточек «было → стало», сверху фильтры."""
    ctx.need("audit.view")
    from ..services import audit_view as av
    flt = _audit_filters(user, section, action, period, f, t, limit, p, d, via)
    rows, more = av.fetch(ctx.db, ctx.branch.id, flt)
    counts = av.counts_by_user(ctx.db, ctx.branch.id, flt)
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id)
                           .order_by(User.active.desc(), User.full_name)).all()
    staff = [u for u in staff if u.active or counts.get(u.id)]
    items = av.describe(ctx.db, rows)
    days: list[tuple] = []
    for it in items:
        d = it.row.created_at.astimezone().date()
        if not days or days[-1][0] != d:
            days.append((d, []))
        days[-1][1].append(it)
    chosen = next((u for u in staff if str(u.id) == flt.user), None)
    # Плитки как на «Пульте» (11.11.2026): сколько действий каждого вида при тех же фильтрах
    import dataclasses
    from urllib.parse import urlencode
    from sqlalchemy import func
    sub = av.base_query(ctx.branch.id, dataclasses.replace(flt, action="")).subquery()
    by_action = {}
    for a, n in ctx.db.execute(select(sub.c.action, func.count()).group_by(sub.c.action)):
        k = a if a in ("create", "update", "delete") else ("login" if a in av.LOGIN_ACTIONS else "other")
        by_action[k] = by_action.get(k, 0) + n
    act_tiles = [{"t": "Все действия", "n": sum(by_action.values()), "c": "gray", "on": not flt.action,
                  "href": "/audit?" + flt.query_string(action="")}]
    for k, c in (("create", "green"), ("update", "blue"), ("delete", "red"), ("login", "purple")):
        act_tiles.append({"t": av.ACTION_FILTERS[k], "n": by_action.get(k, 0), "c": c, "on": flt.action == k,
                          "href": "/audit?" + flt.query_string(action=k)})
    # 12.11.2026: что админ сделал «глазами сотрудника»
    n_via = ctx.db.scalar(select(func.count()).select_from(sub).where(sub.c.via_admin_id.is_not(None))) or 0
    act_tiles.append({"t": "👑 Админ за сотрудника", "n": n_via, "c": "orange", "on": flt.via == "admin", "zero": not n_via,
                      "s": "глазами сотрудника", "href": "/audit?" + flt.query_string(via="" if flt.via == "admin" else "admin")})
    return ctx.render("audit.html", title="Журнал", flt=flt, days=days, more=more, counts=counts, staff=staff, act_tiles=act_tiles,
                      total=sum(counts.values()), chosen=chosen, SECTIONS=av.SECTIONS, ACTION_FILTERS=av.ACTION_FILTERS,
                      PERIODS=av.PERIODS, n=len(items), bar=Bar(flt.per(), "/audit", flt.keep()))


@router.get("/audit/print")
def audit_print(ctx: Ctx = Depends(get_ctx), user: str = "", section: str = "", action: str = "", period: str = "week",
                f: str = "", t: str = "", p: str = "", d: str = ""):
    ctx.need("audit.view")
    from ..services import audit_view as av
    flt = _audit_filters(user, section, action, period, f, t, 0, p, d)
    rows, more = av.fetch(ctx.db, ctx.branch.id, flt, limit=2000)
    chosen = ctx.db.get(User, _uid(flt.user)) if flt.user not in ("", "system") else None
    return ctx.render("audit_print.html", title="Журнал действий", items=av.describe(ctx.db, rows), more=more,
                      flt=flt, chosen=chosen, SECTIONS=av.SECTIONS, ACTION_FILTERS=av.ACTION_FILTERS, PERIODS=av.PERIODS,
                      changes_text=av.changes_text)


@router.get("/audit/export.xlsx")
def audit_xlsx(ctx: Ctx = Depends(get_ctx), user: str = "", section: str = "", action: str = "", period: str = "week",
               f: str = "", t: str = "", p: str = "", d: str = ""):
    """Выгрузка журнала в Excel (до 5000 строк) — браузер предложит сохранить файл на ПК."""
    ctx.need("audit.view")
    import io
    from fastapi.responses import Response
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from ..services import audit_view as av
    flt = _audit_filters(user, section, action, period, f, t, 0, p, d)
    rows, _ = av.fetch(ctx.db, ctx.branch.id, flt, limit=5000)
    wb = Workbook()
    ws = wb.active
    ws.title = "Журнал"
    head = ["Дата", "Время", "Сотрудник", "Действие", "Раздел", "Что", "Объект", "Изменения"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEEEF2")
    for it in av.describe(ctx.db, rows):
        at = it.row.created_at.astimezone()
        ws.append([at.strftime("%d.%m.%Y"), at.strftime("%H:%M:%S"), it.who, it.verb, it.section, it.what, it.target,
                   av.changes_text(it)])
    for col, w in zip("ABCDEFGH", (11, 9, 24, 14, 20, 22, 30, 80)):
        ws.column_dimensions[col].width = w
    for r in ws.iter_rows(min_row=2, min_col=8, max_col=8):
        r[0].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    d1, d2 = flt.dates()
    name = f"zhurnal_{d1 or 'nachalo'}_{d2 or date.today()}.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _uid(v: str):
    import uuid
    try:
        return uuid.UUID(v)
    except ValueError:
        return None