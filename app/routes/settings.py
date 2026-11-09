"""Настройки: бренд, номер наряда, роли и права, сотрудники и карты."""
import re
import secrets
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..config import MEDIA_DIR
from ..models import Permission, Role, RolePermission, StaffCard, User, UserPermission
from ..permissions import ADMIN_ROLE, SYSTEM_COLORS, effective_permissions
from ..security import hash_secret
from ..services import numbering
from ..services import settings as st
from ..web import Ctx, Forbidden, flash, get_ctx

router = APIRouter(prefix="/settings")

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}  # SVG не берём: в нём может быть скрипт


def _color(value: str) -> str | None:
    return value if value in SYSTEM_COLORS or HEX.match(value or "") else None


def _perm_groups(ctx: Ctx) -> dict[str, list[Permission]]:
    groups: dict[str, list[Permission]] = {}
    for p in ctx.db.scalars(select(Permission)):
        groups.setdefault(p.group, []).append(p)
    return groups


def _back(path: str):
    return RedirectResponse(path, status_code=303)


# ---------- Главная настроек ----------
@router.get("")
def index(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access", "users.manage", "staff.dismiss")
    if not ctx.can("settings.access", "users.manage"):
        return _back("/settings/users")  # директор: только сотрудники и архив
    n_users = ctx.db.scalar(select(func.count()).select_from(User).where(User.branch_id == ctx.branch.id))
    return ctx.render("settings/index.html", title="Настройки", n_users=n_users)


# ---------- Бренд ----------
@router.get("/branding")
def branding_form(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    return ctx.render("settings/branding.html", title="Оформление",
                      session_cfg=st.get(ctx.db, ctx.branch.id, "session"))


async def _save_image(upload: UploadFile, prefix: str) -> str | None:
    if not upload or not upload.filename:
        return None
    ext = IMG_EXT.get(upload.content_type or "")
    data = await upload.read()
    if not ext or len(data) > 3 * 1024 * 1024:
        return ""
    folder = MEDIA_DIR / "branding"
    folder.mkdir(exist_ok=True)
    name = f"{prefix}-{secrets.token_hex(4)}.{ext}"
    (folder / name).write_bytes(data)
    return f"/media/branding/{name}"


@router.post("/branding")
async def branding_save(ctx: Ctx = Depends(get_ctx), name: str = Form(""), accent: str = Form("blue"),
                        idle_minutes: int = Form(60), logo: UploadFile = File(None), icon: UploadFile = File(None),
                        remove_logo: str = Form("")):
    ctx.need("settings.access")
    value = {"name": name.strip()[:60] or "СТО"}
    if _color(accent):
        value["accent"] = accent
    for field, upload in (("logo", logo), ("icon", icon)):
        path = await _save_image(upload, field)
        if path == "":
            flash(ctx.request, "Картинка: только PNG, JPG или WEBP до 3 МБ", "error")
            return _back("/settings/branding")
        if path:
            value[field] = path
    if remove_logo:
        value["logo"] = ""
    st.put(ctx.db, ctx.branch.id, "branding", value)
    st.put(ctx.db, ctx.branch.id, "session", {"idle_minutes": max(0, min(idle_minutes, 24 * 60))})
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _back("/settings/branding")


# ---------- Номер наряда ----------
@router.get("/numbering")
def numbering_form(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    cfg = st.get(ctx.db, ctx.branch.id, "order_number")
    return ctx.render("settings/numbering.html", title="Номер наряда", cfg=cfg,
                      example=numbering.preview(cfg["template"], ctx.branch.code))


@router.post("/numbering")
def numbering_save(ctx: Ctx = Depends(get_ctx), template: str = Form(...), code: str = Form(...),
                   reset_yearly: str = Form("")):
    ctx.need("settings.access")
    template, code = template.strip(), code.strip()
    if "{номер" not in template:
        flash(ctx.request, "В шаблоне обязательно должен быть {номер}", "error")
        return _back("/settings/numbering")
    if not code or len(code) > 20:
        flash(ctx.request, "Код СТО: от 1 до 20 символов", "error")
        return _back("/settings/numbering")
    ctx.branch.code = code
    st.put(ctx.db, ctx.branch.id, "order_number", {"template": template[:80], "reset_yearly": bool(reset_yearly)})
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _back("/settings/numbering")


# ---------- Роли и права ----------
@router.get("/roles")
def roles(ctx: Ctx = Depends(get_ctx)):
    ctx.need("users.manage")
    rows = ctx.db.scalars(select(Role).where(Role.branch_id == ctx.branch.id).order_by(Role.sort)).all()
    counts = dict(ctx.db.execute(select(User.role_id, func.count()).where(User.active.is_(True)).group_by(User.role_id)).all())
    return ctx.render("settings/roles.html", title="Роли и доступ", roles=rows, counts=counts)


def _role(ctx: Ctx, role_id: str) -> Role:
    try:
        role = ctx.db.get(Role, uuid.UUID(role_id))
    except ValueError:
        raise Forbidden()
    if not role or role.branch_id != ctx.branch.id:
        raise Forbidden()
    return role


@router.get("/roles/{role_id}")
def role_view(role_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("users.manage")
    role = _role(ctx, role_id)
    on = set(ctx.db.scalars(select(RolePermission.permission).where(RolePermission.role_id == role.id)))
    return ctx.render("settings/role.html", title=role.name, role=role, on=on, groups=_perm_groups(ctx),
                      is_admin=role.code == ADMIN_ROLE)


@router.post("/roles/{role_id}/color")
def role_color(role_id: str, ctx: Ctx = Depends(get_ctx), color: str = Form(...), name: str = Form("")):
    ctx.need("users.manage")
    role = _role(ctx, role_id)
    if _color(color):
        role.color = color
    if name.strip():
        role.name = name.strip()[:80]
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _back(f"/settings/roles/{role_id}")


@router.post("/roles/{role_id}/perm")
def role_perm(role_id: str, ctx: Ctx = Depends(get_ctx), perm: str = Form(...), on: str = Form("")):
    """Переключатель права у роли. Вызывается из JS без перезагрузки страницы."""
    ctx.need("users.manage")
    role = _role(ctx, role_id)
    if role.code == ADMIN_ROLE:
        return JSONResponse({"ok": False, "error": "У админа все права всегда"}, status_code=400)
    if not ctx.db.get(Permission, perm):
        return JSONResponse({"ok": False}, status_code=400)
    row = ctx.db.scalar(select(RolePermission).where(RolePermission.role_id == role.id, RolePermission.permission == perm))
    if on and not row:
        ctx.db.add(RolePermission(role_id=role.id, permission=perm))
    elif not on and row:
        ctx.db.delete(row)
    ctx.db.commit()
    return {"ok": True}


# ---------- Сотрудники ----------
@router.get("/users")
def users(ctx: Ctx = Depends(get_ctx), tab: str = ""):
    """Сотрудники: вкладки «Работают» и «Архив» (уволенные — вся их история сохраняется)."""
    ctx.need("users.manage", "staff.dismiss")
    rows = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id)
                          .order_by(User.dismissed_on.desc(), User.full_name)).all()
    working = [u for u in rows if u.active]
    archive = [u for u in rows if not u.active]
    roles = ctx.db.scalars(select(Role).where(Role.branch_id == ctx.branch.id).order_by(Role.sort)).all()
    tab = "archive" if tab == "archive" else "work"
    return ctx.render("settings/users.html", title="Сотрудники", users=archive if tab == "archive" else working,
                      tab=tab, n_work=len(working), n_archive=len(archive), roles=roles)


def _date(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


LOGIN_RE = re.compile(r"^[a-z0-9._-]{2,40}$")


@router.post("/users")
def user_create(ctx: Ctx = Depends(get_ctx), full_name: str = Form(...), login: str = Form(...),
                role_id: str = Form(...), password: str = Form(""), pin: str = Form("")):
    ctx.need("users.manage")
    login = login.strip().lower()
    err = _check_login(ctx, login) or _check_pin(pin) or _check_password(password)
    if err:
        flash(ctx.request, err, "error")
        return _back("/settings/users")
    role = _role(ctx, role_id)
    u = User(full_name=full_name.strip()[:120], login=login, role_id=role.id,
             password_hash=hash_secret(password) if password else "", pin_hash=hash_secret(pin) if pin else "")
    ctx.db.add(u)
    ctx.db.commit()
    flash(ctx.request, "Сотрудник добавлен")
    return _back(f"/settings/users/{u.id}")


def _check_login(ctx: Ctx, login: str, exclude=None) -> str:
    if not LOGIN_RE.match(login):
        return "Логин: латиница, цифры, точка, дефис — от 2 символов"
    q = select(User).where(User.login == login)
    if exclude:
        q = q.where(User.id != exclude)
    return "Такой логин уже есть" if ctx.db.scalar(q) else ""


def _check_pin(pin: str) -> str:
    # Ровно 4 цифры: клавиатура на iPhone входит сама после 4-й цифры
    return "" if not pin or (pin.isdigit() and len(pin) == 4) else "PIN: ровно 4 цифры"


def _check_password(pwd: str) -> str:
    return "" if not pwd or len(pwd) >= 6 else "Пароль: минимум 6 символов"


def _user(ctx: Ctx, user_id: str) -> User:
    try:
        u = ctx.db.get(User, uuid.UUID(user_id))
    except ValueError:
        raise Forbidden()
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    return u


@router.get("/users/{user_id}")
def user_view(user_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    roles = ctx.db.scalars(select(Role).where(Role.branch_id == ctx.branch.id).order_by(Role.sort)).all()
    role_perms = set(ctx.db.scalars(select(RolePermission.permission).where(RolePermission.role_id == u.role_id)))
    overrides = {p.permission: p.allow for p in ctx.db.scalars(select(UserPermission).where(UserPermission.user_id == u.id))}
    cards = ctx.db.scalars(select(StaffCard).where(StaffCard.user_id == u.id).order_by(StaffCard.created_at)).all()
    from ..services.tools import holding
    held = holding(ctx.db, u.id)
    return ctx.render("settings/user.html", title=u.full_name, u=u, roles=roles, groups=_perm_groups(ctx),
                      n_tools=len(held), n_tools_temp=sum(1 for t in held if not t.is_personal),
                      role_perms=role_perms, overrides=overrides, cards=cards,
                      effective=effective_permissions(ctx.db, u), is_admin=u.role.code == ADMIN_ROLE)


@router.post("/users/{user_id}")
def user_save(user_id: str, ctx: Ctx = Depends(get_ctx), full_name: str = Form(...), login: str = Form(...),
              role_id: str = Form(...), phone: str = Form(""), hired_on: str = Form("")):
    """Профиль. Увольнение — отдельной кнопкой (страница «Уволить»), тут не меняется."""
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    login = login.strip().lower()
    if err := _check_login(ctx, login, exclude=u.id):
        flash(ctx.request, err, "error")
        return _back(f"/settings/users/{user_id}")
    new_role = _role(ctx, role_id)
    # Себя нельзя лишить роли админа — иначе можно потерять доступ к настройкам
    if u.id == ctx.user.id and u.role.code == ADMIN_ROLE and new_role.code != ADMIN_ROLE:
        flash(ctx.request, "Нельзя снять с себя роль админа", "error")
        return _back(f"/settings/users/{user_id}")
    u.full_name, u.login, u.role_id, u.phone = full_name.strip()[:120], login, new_role.id, phone.strip()[:40]
    if (d := _date(hired_on)) and d != u.hired_date:
        u.hired_on = d
    ctx.db.commit()
    flash(ctx.request, "Сохранено")
    return _back(f"/settings/users/{user_id}")


@router.post("/users/{user_id}/secret")
def user_secret(user_id: str, ctx: Ctx = Depends(get_ctx), password: str = Form(""), pin: str = Form("")):
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    if err := (_check_password(password) or _check_pin(pin)):
        flash(ctx.request, err, "error")
        return _back(f"/settings/users/{user_id}")
    if password:
        u.password_hash = hash_secret(password)
    if pin:
        u.pin_hash = hash_secret(pin)
    ctx.db.commit()
    flash(ctx.request, "Пароль / PIN обновлён" if (password or pin) else "Ничего не изменено")
    return _back(f"/settings/users/{user_id}")


# ---------- Увольнение, возврат, архив ----------
@router.get("/users/{user_id}/dismiss")
def dismiss_form(user_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("staff.dismiss")
    u = _user(ctx, user_id)
    if not u.active:
        return _back(f"/settings/users/{u.id}/archive")
    from ..models import Vehicle
    from ..models.orders import WO_STATUSES
    from ..services import orders as osvc
    from ..services import staff as ssvc
    from ..services.tools import holding
    orders = ssvc.open_orders(ctx.db, u)
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o, _ in orders])))}
    return ctx.render("settings/dismiss.html", title=f"Уволить — {u.full_name}", u=u, orders=orders, vehicles=vehicles,
                      tools=holding(ctx.db, u.id), appts=ssvc.future_appointments(ctx.db, u),
                      cands=ssvc.candidates(ctx.db, u), SLOTS=ssvc.SLOTS, STATUSES=WO_STATUSES,
                      fmt_plate=osvc.fmt_plate, today=date.today(), is_me=u.id == ctx.user.id)


@router.post("/users/{user_id}/dismiss")
async def dismiss_save(user_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("staff.dismiss")
    u = _user(ctx, user_id)
    form = await ctx.request.form()
    if u.id == ctx.user.id:
        flash(ctx.request, "Себя уволить нельзя", "error")
        return _back(f"/settings/users/{u.id}/dismiss")
    if not u.active:
        return _back(f"/settings/users/{u.id}/archive")
    from ..services import staff as ssvc
    from ..services.tools import holding
    if held := holding(ctx.db, u.id):
        flash(ctx.request, f"За сотрудником числится инструмент ({len(held)} шт.) — сначала проведите проверку при увольнении", "error")
        return _back(f"/settings/users/{u.id}/dismiss")
    on = _date(str(form.get("dismissed_on", ""))) or date.today()
    moved = ssvc.dismiss(ctx.db, u, ctx.user, on, str(form.get("reason", "")), dict(form))
    ctx.db.commit()
    flash(ctx.request, f"Сотрудник {u.full_name} уволен и перенесён в архив" + (f". Передано нарядов: {moved}" if moved else ""))
    return _back(f"/settings/users/{u.id}/archive")


@router.post("/users/{user_id}/rehire")
def rehire(user_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("staff.dismiss")
    u = _user(ctx, user_id)
    if not u.active:
        from ..services import staff as ssvc
        ssvc.rehire(ctx.db, u, date.today())
        ctx.db.commit()
        flash(ctx.request, f"{u.full_name} снова в работе")
    return _back(f"/settings/users/{u.id}" if ctx.can("users.manage") else "/settings/users")


ARCHIVE_TABS = {"orders": "Наряды", "works": "Работы", "time": "Табель", "pay": "Зарплата", "tools": "Инструмент"}


@router.get("/users/{user_id}/archive")
def archive_view(user_id: str, ctx: Ctx = Depends(get_ctx), tab: str = "orders"):
    """Всё по сотруднику за всё время: наряды, выполненные работы, табель, зарплата, инструмент."""
    ctx.need("users.manage", "staff.dismiss")
    u = _user(ctx, user_id)
    db = ctx.db
    from sqlalchemy import or_
    from ..models import PayrollAccrual, PayrollPayout, ToolCheck, ToolMove, Tool, Vehicle, WorkOrder, WoWork
    from ..models.orders import WO_STATUSES
    from ..models.tools import CHECK_KINDS, CHECK_STATUS
    from ..services import orders as osvc
    from ..services import payroll as psvc
    from ..services import staff as ssvc
    from ..services.attendance import month_grid
    d1 = u.hired_date
    d2 = u.dismissed_on or date.today()
    tabs = {k: v for k, v in ARCHIVE_TABS.items()
            if (k != "pay" or ctx.can("payroll.view_all")) and (k != "time" or ctx.can("attendance.view_all"))}
    tab = tab if tab in tabs else "orders"

    works = db.scalars(select(WoWork).where(WoWork.mechanic_id == u.id, WoWork.status == "done")
                       .order_by(WoWork.done_at.desc())).all()
    work_wo = {w.work_order_id for w in works}
    orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, or_(
        WorkOrder.mechanic_id == u.id, WorkOrder.manager_id == u.id, WorkOrder.partsman_id == u.id,
        WorkOrder.id.in_(work_wo))).order_by(WorkOrder.created_at.desc())).all()
    omap = {o.id: o for o in orders}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))}
    hours = sum((Decimal(w.hours or 0) * Decimal(w.qty or 1) for w in works), Decimal(0))

    # Табель и зарплата — по месяцам, от приёма до сегодня (после увольнения бывает расчёт)
    last = date.today() if date.today() > d2 else d2
    months = []
    y, m = d1.year, d1.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    time_rows, days_total, minutes_total = [], 0, 0
    if "time" in tabs:
        for y, m in months:
            grid = month_grid(db, ctx.branch.id, [u], y, m)[u.id]
            days = [d for d in grid.values() if d.minutes or d.open]
            if days:
                mins = sum(d.minutes for d in days)
                time_rows.append((date(y, m, 1), len(days), mins))
                days_total, minutes_total = days_total + len(days), minutes_total + mins
    pay_rows, pay = [], None
    if "pay" in tabs:
        acc = db.scalars(select(PayrollAccrual).where(PayrollAccrual.user_id == u.id, PayrollAccrual.status != "cancelled")).all()
        outs = db.scalars(select(PayrollPayout).where(PayrollPayout.user_id == u.id)).all()
        by = {}
        for a in acc:
            r = by.setdefault(a.on_date.replace(day=1), [Decimal(0), Decimal(0)])
            r[0] += a.amount
        for p in outs:
            r = by.setdefault(p.paid_on.replace(day=1), [Decimal(0), Decimal(0)])
            r[1] += p.amount
        from calendar import monthrange
        pay_rows = sorted(((k, k.replace(day=monthrange(k.year, k.month)[1]), a, p) for k, (a, p) in by.items()),
                          reverse=True)
        accrued, paid = sum((a.amount for a in acc), Decimal(0)), sum((p.amount for p in outs), Decimal(0))
        pay = {"accrued": accrued, "paid": paid, "due": accrued - paid}
    checks = db.scalars(select(ToolCheck).where(ToolCheck.user_id == u.id).order_by(ToolCheck.created_at.desc())).all()
    moves = db.scalars(select(ToolMove).where(or_(ToolMove.to_user == u.id, ToolMove.from_user == u.id))
                       .order_by(ToolMove.created_at.desc())).all()
    tools = {t.id: t for t in db.scalars(select(Tool).where(Tool.id.in_([mv.tool_id for mv in moves])))}
    by_user = db.get(User, u.dismissed_by) if u.dismissed_by else None
    return ctx.render("settings/archive.html", title=u.full_name, u=u, tab=tab, tabs=tabs, d1=d1, d2=d2,
                      tenure=ssvc.tenure(d1, d2), by_user=by_user, orders=orders, omap=omap, vehicles=vehicles,
                      works=works, hours=hours, time_rows=time_rows, days_total=days_total,
                      minutes_total=minutes_total, pay_rows=pay_rows, pay=pay, checks=checks, moves=moves,
                      tools=tools, STATUSES=WO_STATUSES, CHECK_KINDS=CHECK_KINDS, CHECK_STATUS=CHECK_STATUS,
                      fmt_plate=osvc.fmt_plate, MONTHS=MONTHS_RU)


MONTHS_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
             "Ноябрь", "Декабрь"]


@router.post("/users/{user_id}/cards")
def card_add(user_id: str, ctx: Ctx = Depends(get_ctx), uid: str = Form(...), label: str = Form("")):
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    uid = uid.strip()
    if not uid or len(uid) > 64:
        flash(ctx.request, "Номер карты пустой", "error")
    elif ctx.db.scalar(select(StaffCard).where(StaffCard.uid == uid)):
        flash(ctx.request, "Эта карта уже привязана к сотруднику", "error")
    else:
        ctx.db.add(StaffCard(user_id=u.id, uid=uid, label=label.strip()[:80]))
        ctx.db.commit()
        flash(ctx.request, "Карта привязана")
    return _back(f"/settings/users/{user_id}")


@router.post("/users/{user_id}/cards/{card_id}/toggle")
def card_toggle(user_id: str, card_id: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    card = ctx.db.get(StaffCard, uuid.UUID(card_id))
    if card and card.user_id == u.id:
        card.active = not card.active
        ctx.db.commit()
    return _back(f"/settings/users/{user_id}")


@router.post("/users/{user_id}/perm")
def user_perm(user_id: str, ctx: Ctx = Depends(get_ctx), perm: str = Form(...), mode: str = Form(...)):
    """mode: role — как у роли, allow — разрешить, deny — запретить."""
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    if not ctx.db.get(Permission, perm) or mode not in ("role", "allow", "deny"):
        return JSONResponse({"ok": False}, status_code=400)
    row = ctx.db.scalar(select(UserPermission).where(UserPermission.user_id == u.id, UserPermission.permission == perm))
    if mode == "role":
        if row:
            ctx.db.delete(row)
    elif row:
        row.allow = mode == "allow"
    else:
        ctx.db.add(UserPermission(user_id=u.id, permission=perm, allow=mode == "allow"))
    ctx.db.commit()
    return {"ok": True, "effective": perm in effective_permissions(ctx.db, u)}


# ---------- Подъёмники (посты) ----------
@router.get("/lifts")
def lifts_view(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    from ..models import Lift
    from ..services.orders import staff_by_role
    lifts = ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id).order_by(Lift.number)).all()
    mechanics = staff_by_role(ctx.db, ctx.branch.id, "mechanic")
    return ctx.render("settings/lifts.html", title="Подъёмники", lifts=lifts, mechanics=mechanics,
                      by_id={m.id: m for m in mechanics})


@router.post("/lifts/{lift_id}")
def lift_save(lift_id: str, ctx: Ctx = Depends(get_ctx), name: str = Form(""), mechanic_id: str = Form(""),
              active: str = Form("")):
    ctx.need("settings.access")
    from ..models import Lift
    lift = ctx.db.get(Lift, uuid.UUID(lift_id))
    if not lift or lift.branch_id != ctx.branch.id:
        raise Forbidden()
    lift.name = name.strip()[:60] or f"Пост {lift.number}"
    mech = ctx.db.get(User, uuid.UUID(mechanic_id)) if mechanic_id else None
    lift.mechanic_id = mech.id if mech and mech.branch_id == ctx.branch.id and mech.role.code == "mechanic" else None
    lift.active = bool(active)
    ctx.db.commit()
    flash(ctx.request, f"Подъёмник {lift.number} сохранён")
    return _back("/settings/lifts")


@router.post("/lifts")
def lift_add(ctx: Ctx = Depends(get_ctx)):
    ctx.need("settings.access")
    from ..models import Lift
    n = (ctx.db.scalar(select(func.max(Lift.number)).where(Lift.branch_id == ctx.branch.id)) or 0) + 1
    ctx.db.add(Lift(number=n, name=f"Пост {n}"))
    ctx.db.commit()
    flash(ctx.request, f"Добавлен подъёмник {n}")
    return _back("/settings/lifts")
