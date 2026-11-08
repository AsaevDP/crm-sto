"""Вход: логин+пароль, сотрудник+PIN (iPhone), карта (считыватель у ПК)."""
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import current_branch_id, current_user_id, get_db
from ..models import Branch, StaffCard, User
from ..security import check_secret, is_locked, register_fail, reset_fails
from ..services import settings as st
from ..web import accent_style, templates

router = APIRouter()


def _branding(db: Session) -> dict:
    branch = db.scalar(select(Branch))
    return st.get(db, branch.id, "branding") if branch else {}


def _login_page(request: Request, db: Session, error: str = "", tab: str = "", status: int = 200):
    branding = _branding(db)
    staff = db.scalars(select(User).where(User.active.is_(True), User.pin_hash != "").order_by(User.full_name)).all()
    return templates.TemplateResponse(request, "login.html", {
        "branding": branding, "accent": accent_style(branding.get("accent", "blue")),
        "staff": staff, "error": error, "tab": tab,
    }, status_code=status)


def _start_session(request: Request, db: Session, user: User, method: str):
    request.session.clear()
    request.session["uid"] = str(user.id)
    request.session["seen"] = time.time()
    current_user_id.set(user.id)
    current_branch_id.set(user.branch_id)
    audit.write(db, "users", user.id, "login", {"способ": method})
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return _login_page(request, db)


@router.post("/login")
def login_password(request: Request, login: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    key = f"login:{login.lower()}"
    if wait := is_locked(key):
        return _login_page(request, db, f"Слишком много попыток. Подождите {wait} сек.", "password", 429)
    user = db.scalar(select(User).where(User.login == login.strip().lower(), User.active.is_(True)))
    if not user or not check_secret(password, user.password_hash):
        register_fail(key)
        return _login_page(request, db, "Неверный логин или пароль", "password", 401)
    reset_fails(key)
    return _start_session(request, db, user, "пароль")


@router.post("/login/pin")
def login_pin(request: Request, user_id: str = Form(""), pin: str = Form(""), db: Session = Depends(get_db)):
    key = f"pin:{user_id}"
    if wait := is_locked(key):
        return _login_page(request, db, f"Слишком много попыток. Подождите {wait} сек.", "pin", 429)
    user = db.scalar(select(User).where(User.active.is_(True), User.id == _uuid(user_id)))
    if not user or not check_secret(pin, user.pin_hash):
        register_fail(key)
        return _login_page(request, db, "Неверный PIN", "pin", 401)
    reset_fails(key)
    return _start_session(request, db, user, "PIN")


@router.post("/login/card")
def login_card(request: Request, uid: str = Form(""), db: Session = Depends(get_db)):
    uid = uid.strip()
    key = f"card:{request.client.host if request.client else ''}"
    if wait := is_locked(key):
        return _login_page(request, db, f"Слишком много попыток. Подождите {wait} сек.", "card", 429)
    card = db.scalar(select(StaffCard).where(StaffCard.uid == uid, StaffCard.active.is_(True)))
    user = db.get(User, card.user_id) if card else None
    if not user or not user.active:
        register_fail(key)
        return _login_page(request, db, "Карта не найдена или заблокирована", "card", 401)
    reset_fails(key)
    return _start_session(request, db, user, "карта")


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    if uid:
        user = db.get(User, _uuid(uid))
        if user:
            current_user_id.set(user.id)
            current_branch_id.set(user.branch_id)
            audit.write(db, "users", user.id, "logout")
            db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _uuid(v: str):
    import uuid
    try:
        return uuid.UUID(v)
    except ValueError:
        return None


# ---------- «Глазами сотрудника» (только главный админ) ----------
def _real_admin(request: Request, db: Session) -> User:
    from ..web import Forbidden, NotLoggedIn
    uid = request.session.get("uid")
    me = db.get(User, _uuid(uid)) if uid else None
    if not me:
        raise NotLoggedIn()
    if me.role.code != "admin":
        raise Forbidden()
    current_user_id.set(me.id)
    current_branch_id.set(me.branch_id)
    return me


@router.get("/view-as")
def view_as_list(request: Request, db: Session = Depends(get_db)):
    from ..web import get_ctx  # noqa: F401
    me = _real_admin(request, db)
    from ..models import Role
    staff = db.scalars(select(User).join(Role, User.role_id == Role.id)
                       .where(User.branch_id == me.branch_id, User.active.is_(True), User.id != me.id)
                       .order_by(Role.sort, User.full_name)).all()
    from ..services import settings as st_
    from ..web import accent_style as acc
    return templates.TemplateResponse(request, "view_as.html", {
        "staff": staff, "me": me, "branding": st_.get(db, me.branch_id, "branding"),
        "accent": acc(me.role.color), "flash": request.session.pop("flash", None)})


@router.post("/view-as/stop")
def view_as_stop(request: Request, db: Session = Depends(get_db)):
    if request.session.pop("as_uid", None):
        me = _real_admin(request, db)
        audit.write(db, "users", me.id, "view_as_stop")
        db.commit()
    return RedirectResponse("/view-as", status_code=303)


@router.post("/view-as/{user_id}")
def view_as_start(user_id: str, request: Request, db: Session = Depends(get_db)):
    me = _real_admin(request, db)
    target = db.get(User, _uuid(user_id))
    if not target or target.branch_id != me.branch_id or not target.active:
        return RedirectResponse("/view-as", status_code=303)
    request.session["as_uid"] = str(target.id)
    audit.write(db, "users", target.id, "view_as", {"кто смотрит": me.full_name, "глазами": target.full_name})
    db.commit()
    return RedirectResponse("/", status_code=303)
