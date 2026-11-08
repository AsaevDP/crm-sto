"""Общее для всех экранов: шаблоны, текущий пользователь, проверка прав, цвета."""
import time
import uuid
from dataclasses import dataclass, field

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import DEMO, ROOT
from .db import current_branch_id, current_user_id, get_db
from .models import Branch, User
from .permissions import SYSTEM_COLORS, effective_permissions, menu_for
from .services import settings as st

templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


class NotLoggedIn(Exception):
    pass


class Forbidden(Exception):
    pass


def accent_style(color: str) -> str:
    """CSS для акцента: системный цвет (адаптируется к тёмной теме) или свой #hex."""
    if color in SYSTEM_COLORS:
        on = "var(--bg)" if color in ("graphite", "yellow") else "#fff"
        return f"--accent: var(--{color}); --on-accent: {on};"
    hexv = color.lstrip("#")
    try:
        r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return ""
    # Яркий цвет — тёмный текст на кнопках, тёмный — белый
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return f"--accent: #{hexv}; --on-accent: {'#1c1c1e' if lum > 170 else '#fff'};"


def color_css(color: str) -> str:
    """Просто цвет для превью-кружка."""
    return f"var(--{color})" if color in SYSTEM_COLORS else color


def asset(path: str) -> str:
    """/static/... с меткой версии (время изменения файла), чтобы браузер и iPhone
    не держали старый CSS/JS из кэша после обновления."""
    f = ROOT / "app" / path.lstrip("/")
    try:
        return f"{path}?v={int(f.stat().st_mtime)}"
    except OSError:
        return path


templates.env.globals.update(color_css=color_css, SYSTEM_COLORS=SYSTEM_COLORS, asset=asset, DEMO=DEMO)


@dataclass
class Ctx:
    request: Request
    db: Session
    user: User
    branch: Branch
    perms: set[str]
    branding: dict
    menu: list = field(default_factory=list)

    def can(self, *codes: str) -> bool:
        return any(c in self.perms for c in codes)

    def need(self, *codes: str):
        if not self.can(*codes):
            raise Forbidden()

    def render(self, template: str, **kw):
        kw.setdefault("active", self.request.url.path)
        return templates.TemplateResponse(self.request, template, {
            "ctx": self, "user": self.user, "branding": self.branding, "menu": self.menu,
            "accent": accent_style(self.user.role.color), "role_code": self.user.role.code,
            "flash": self.request.session.pop("flash", None), **kw,
        })


def flash(request: Request, text: str, kind: str = "ok"):
    request.session["flash"] = {"text": text, "kind": kind}


async def get_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    # async — чтобы current_user_id/current_branch_id «доехали» до обработчика экрана
    uid = request.session.get("uid")
    if not uid:
        raise NotLoggedIn()
    user = db.get(User, uuid.UUID(uid))
    if user is None or not user.active:
        request.session.clear()
        raise NotLoggedIn()
    branding = st.get(db, user.branch_id, "branding")
    idle = st.get(db, user.branch_id, "session")["idle_minutes"]
    now = time.time()
    if idle and now - request.session.get("seen", now) > idle * 60:
        request.session.clear()
        raise NotLoggedIn()
    request.session["seen"] = now

    current_user_id.set(user.id)
    current_branch_id.set(user.branch_id)
    perms = effective_permissions(db, user)
    return Ctx(request=request, db=db, user=user, branch=db.get(Branch, user.branch_id),
               perms=perms, branding=branding, menu=menu_for(perms))
