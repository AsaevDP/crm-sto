"""Решения руководителя и «директора нет — решаю сам» (12.11.2026, решение брата).

Решения директора / зама: доступ менеджера ко всем нарядам, доступ к чужому клиенту, «открыть заново», ремонт по гарантии.
Если по табелю ни директора, ни зама нет в здании (ушли по карте или не приходили) — тот, кто просил, решает сам:
  • доступ ко всем нарядам — только на 1 час;
  • доступ к клиенту — через поиск по точным данным (на 1 час);
  • «открыть заново» — только наряд, закрытый не больше суток назад;
  • ремонт по гарантии — НЕ сам: в нём всегда разбирается директор или зам.
Решение помечается (self_decided, в журнале — кто), директору и заму приходит сообщение «решили без вас».
Если директор или зам в здании — их нужно найти (позвонить), сам решать нельзя.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Role, User, Vehicle, WorkOrder

DECIDER_ROLES = ("director", "deputy")
SELF_HOURS = 1            # доступ, который можно дать себе самому
SELF_REOPEN_DAYS = 1      # «открыть заново» сам — только недавно закрытый


def deciders(db: Session, branch_id) -> list[User]:
    return db.scalars(select(User).join(Role, Role.id == User.role_id).where(
        User.branch_id == branch_id, User.active.is_(True), Role.code.in_(DECIDER_ROLES))).all()


def on_site(db: Session, branch_id) -> list[User]:
    """Кто из директора / зама сейчас в здании: последняя отметка за сегодня — «пришёл»."""
    from .attendance import on_site_now
    ds = deciders(db, branch_id)
    last = on_site_now(db, branch_id, ds)
    return [u for u in ds if last.get(u.id) is not None and last[u.id].direction == "in"]


def can_self_decide(db: Session, branch_id) -> bool:
    return bool(deciders(db, branch_id)) and not on_site(db, branch_id)


def absent_note(db: Session, branch_id) -> str:
    names = ", ".join(u.short_name for u in deciders(db, branch_id))
    return f"директора и зама не было на месте по табелю ({names})" if names else "директора и зама не было на месте"


def tell_deciders(db: Session, ctx, title: str, text: str, url: str, wo=None):
    """Сообщение директору и заму: «это решение должны были принять вы — вас не было, его принял(а) …»."""
    from . import notify as nsvc
    nsvc.send(db, [u.id for u in deciders(db, ctx.branch.id)], "self_decided",
              f"Решили без вас: {title}",
              f"Это решение должны были принять вы — вас не было на месте. Его принял(а) {ctx.user.full_name}. {text}".strip(),
              url, wo, skip=ctx.user.id)


def reopen_self_ok(wo: WorkOrder) -> bool:
    if wo.status != "closed" or not wo.closed_at:
        return wo.status == "cancel"
    at = wo.closed_at if wo.closed_at.tzinfo else wo.closed_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at <= timedelta(days=SELF_REOPEN_DAYS)


# ---------- Поиск по точным данным (менеджер) ----------
def exact_clients(db: Session, branch_id, q: str) -> list:
    """Клиенты по ТОЧНЫМ данным: полный госномер, полный телефон, полный VIN или номер наряда.
    По имени и кусочку номера — ничего (чтобы через поиск нельзя было выкачать базу)."""
    from .orders import norm_phone, norm_plate
    q = (q or "").strip()
    if not q:
        return []
    ids = set()
    num = db.scalar(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.number == q.upper()))
    if num:
        ids.add(num.client_id)
    plate = norm_plate(q)
    if plate and len(plate) >= 6:
        for v in db.scalars(select(Vehicle).where(Vehicle.branch_id == branch_id, Vehicle.plate == plate)):
            ids.add(v.client_id)
    vin = q.upper().replace(" ", "")
    if len(vin) == 17:
        for v in db.scalars(select(Vehicle).where(Vehicle.branch_id == branch_id, Vehicle.vin == vin)):
            ids.add(v.client_id)
    phone = norm_phone(q)
    if len(phone) >= 12 or (len("".join(ch for ch in q if ch.isdigit())) >= 10):
        for c in db.scalars(select(Client).where(Client.branch_id == branch_id, Client.phone == phone)):
            ids.add(c.id)
    return [i for i in ids if i]


def grant_by_search(db: Session, ctx, client_ids, q: str) -> int:
    """Менеджер нашёл клиента по точным данным — видит его и все его наряды 1 час. Пишется в журнал (кто, что искал)."""
    from ..audit import write
    from ..db import now
    from ..models import ClientAccess
    from .orders import granted_client_ids
    have = granted_client_ids(ctx)
    n = 0
    for cid in client_ids:
        if cid in have:
            continue
        db.add(ClientAccess(user_id=ctx.user.id, client_id=cid, status="approved", hours=SELF_HOURS,
                            until=now() + timedelta(hours=SELF_HOURS), decided_by=ctx.user.id, decided_at=now(),
                            reason=f"Найден поиском по точным данным: «{q.strip()[:80]}»"))
        n += 1
    write(db, "search", ctx.user.id, "search", {"запрос": q.strip()[:80], "найдено клиентов": len(client_ids)})
    return n
