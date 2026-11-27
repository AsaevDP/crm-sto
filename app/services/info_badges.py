"""Кружки «для сведения» (сиреневая обводка) — выключить (27.11.2026, брат).

Директору, заму, админу дела сотрудников показываются кружком с сиреневой обводкой. Их можно выключить:
- сам руководитель — «Настройки → Мои настройки» (хранится в настройке филиала `info_badges.off` — список id);
- админ — правом `badges.hide_info` у роли или у сотрудника. Тогда сам руководитель включить обратно не может.
У админа это право есть всегда (все права) — для него оно ничего не значит, только своя галочка.
Кружки без обводки (дела самого руководителя) не выключаются никогда.
"""
from . import settings as st

KEY = "info_badges"


def by_admin(ctx) -> bool:
    """Выключено правом, которое ставит админ."""
    return ctx.user.role.code != "admin" and ctx.can("badges.hide_info")


def by_self(db, bid, uid) -> bool:
    return str(uid) in (st.get(db, bid, KEY).get("off") or [])


def hidden(ctx) -> bool:
    return by_admin(ctx) or by_self(ctx.db, ctx.branch.id, ctx.user.id)


def set_self(db, bid, uid, off: bool) -> None:
    ids = [i for i in (st.get(db, bid, KEY).get("off") or []) if i != str(uid)]
    if off:
        ids.append(str(uid))
    st.put(db, bid, KEY, {"off": ids})
