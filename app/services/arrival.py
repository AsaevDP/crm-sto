"""Клиент приехал по записи (25.11.2026, брат).

«✓ Приехали» в «Записи» может нажать ЛЮБОЙ менеджер (и директор, зам, админ — у кого schedule.manage). Наряд создаётся
сразу из записи (клиент, машина, жалоба, мастер), получает крючок для ключей и ждёт оформления приёмки
(`WorkOrder.intake_pending`). Такой наряд можно сохранять незаполненным — это привилегия только приехавших по записи.

Ведёт наряд (и оформляет приёмку) — менеджер, который записывал клиента; записывал не менеджер (директор, зам) —
тот, кто нажал «Приехали». Ведущему — 🔴 кружок «Клиент приехал — оформите приёмку» и сообщение. Мастер и запчастист
наряд не видят, пока ведущий не нажмёт «✓ Принять в работу» — дальше обычный путь (диагностика / подбор).
Нет телефона или госномера в записи — создать не из чего: «Приехали» открывает обычную «Приёмку».
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Appointment, Client, User, Vehicle, WorkOrder
from . import orders as osvc


def can_auto(db: Session, a: Appointment) -> bool:
    """Хватит ли данных записи, чтобы сразу завести наряд: телефон и госномер (или известные клиент и машина)."""
    if a.vehicle_id and a.client_id:
        return True
    return len(osvc.norm_phone(a.phone or "")) >= 10 and bool(osvc.norm_plate(a.plate or ""))


def lead_manager(db: Session, a: Appointment, presser: User) -> User:
    """Кто ведёт наряд: менеджер, который записывал; иначе — кто нажал «Приехали»."""
    u = db.get(User, a.created_by) if a.created_by else None
    if u and u.active and u.role and u.role.code == "manager" and u.branch_id == a.branch_id:
        return u
    return presser


def _form(db: Session, a: Appointment) -> osvc.IntakeForm:
    car = (a.car or "").split(" ", 1)
    f = osvc.IntakeForm(phone=a.phone or "", name=a.client_name or "", plate=a.plate or "",
                        make=car[0] if car and car[0] else "", model=car[1] if len(car) > 1 else "",
                        complaint=a.work or "", mechanic_id=str(a.mechanic_id or ""))
    if a.client_id:
        c = db.get(Client, a.client_id)
        if c:
            f.client_id, f.name, f.phone = str(c.id), c.full_name, f.phone or c.phone
    if a.vehicle_id:
        v = db.get(Vehicle, a.vehicle_id)
        if v:
            f.vehicle_id, f.plate, f.make, f.model = str(v.id), v.plate, v.make, v.model
    f.name = f.name.strip() or osvc.fmt_phone(osvc.norm_phone(f.phone)) or "Клиент"
    return f


def arrive(db: Session, ctx, a: Appointment) -> WorkOrder:
    """Создать наряд по записи (от имени нажавшего — так в журнале). Запись → «приехал», ссылка на наряд."""
    lead = lead_manager(db, a, ctx.user)
    wo = osvc.create_order(db, ctx, _form(db, a))
    wo.manager_id = lead.id
    partsmen = osvc.staff_by_role(db, ctx.branch.id, "partsman")
    p = osvc.paired_partsman(db, lead.id, partsmen)
    wo.partsman_id = p.id if p else None
    wo.intake_pending = True
    wo.arrived_at, wo.arrived_by = now(), ctx.user.id
    a.status, a.work_order_id = "arrived", wo.id
    a.client_id, a.vehicle_id = wo.client_id, wo.vehicle_id
    osvc.event(db, wo, "status", f"Клиент приехал по записи — отметил {ctx.user.short_name}")
    from . import notify
    notify.send(db, [lead.id], "arrived", "Клиент приехал по записи — оформите приёмку",
                f"{wo.number[-6:]} · {notify.car(db, wo)}" + (f" · отметил {ctx.user.short_name}" if lead.id != ctx.user.id else ""),
                url=f"/orders/{wo.id}/intake", wo=wo, skip=None)
    return wo


def can_fill(ctx, wo: WorkOrder) -> bool:
    """Оформляет приёмку ведущий менеджер; директор, зам, админ — тоже (подстраховать)."""
    if ctx.user.role.code == "admin" or ctx.user.id == wo.manager_id:
        return True
    return ctx.user.role.code in ("director", "deputy") and ctx.can("orders.create", "orders.approve")


def pending_for(db: Session, branch_id, user_id) -> list[WorkOrder]:
    """Наряды «клиент приехал», которые оформляет этот менеджер (для кружка)."""
    return db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.intake_pending.is_(True),
                                              WorkOrder.manager_id == user_id, WorkOrder.status == "created")
                      .order_by(WorkOrder.arrived_at)).all()
