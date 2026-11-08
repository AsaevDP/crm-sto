"""Настройки: бренд, номер наряда, роли и права, сотрудники и карты."""
import re
import secrets
import uuid

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
    ctx.need("settings.access", "users.manage")
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
    role = ctx.db.get(Role, uuid.UUID(role_id))
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
def users(ctx: Ctx = Depends(get_ctx)):
    ctx.need("users.manage")
    rows = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id).order_by(User.active.desc(), User.full_name)).all()
    roles = ctx.db.scalars(select(Role).where(Role.branch_id == ctx.branch.id).order_by(Role.sort)).all()
    return ctx.render("settings/users.html", title="Сотрудники", users=rows, roles=roles)


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
    u = ctx.db.get(User, uuid.UUID(user_id))
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
    return ctx.render("settings/user.html", title=u.full_name, u=u, roles=roles, groups=_perm_groups(ctx),
                      role_perms=role_perms, overrides=overrides, cards=cards,
                      effective=effective_permissions(ctx.db, u), is_admin=u.role.code == ADMIN_ROLE)


@router.post("/users/{user_id}")
def user_save(user_id: str, ctx: Ctx = Depends(get_ctx), full_name: str = Form(...), login: str = Form(...),
              role_id: str = Form(...), phone: str = Form(""), active: str = Form("")):
    ctx.need("users.manage")
    u = _user(ctx, user_id)
    login = login.strip().lower()
    if err := _check_login(ctx, login, exclude=u.id):
        flash(ctx.request, err, "error")
        return _back(f"/settings/users/{user_id}")
    new_role = _role(ctx, role_id)
    # Себя нельзя выключить или лишить роли админа — иначе можно потерять доступ к настройкам
    if u.id == ctx.user.id and (not active or (u.role.code == ADMIN_ROLE and new_role.code != ADMIN_ROLE)):
        flash(ctx.request, "Нельзя выключить себя или снять с себя роль админа", "error")
        return _back(f"/settings/users/{user_id}")
    u.full_name, u.login, u.role_id, u.phone, u.active = full_name.strip()[:120], login, new_role.id, phone.strip()[:40], bool(active)
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
