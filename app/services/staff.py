"""Увольнение и возврат сотрудника. Ничего не удаляется: человек помечается уволенным,
не может войти, пропадает из рабочих списков, а вся его история остаётся (вкладка «Архив»)."""
import uuid
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Appointment, Lift, User, WorkOrder, WoWork, WoWorkSession
from ..models.orders import WO_ACTIVE

# Роль сотрудника в наряде: поле наряда → (код роли для замены, подпись)
SLOTS = {"mechanic_id": ("mechanic", "Слесарь"), "manager_id": ("manager", "Менеджер"),
         "partsman_id": ("partsman", "Запчастист")}


def staff_for_period(db: Session, branch_id, date_from: date) -> list[User]:
    """Кто работает сейчас + кто уволен в этом периоде или позже (чтобы рассчитать последний месяц)."""
    return db.scalars(select(User).where(User.branch_id == branch_id, or_(
        User.active.is_(True), User.dismissed_on >= date_from)).order_by(User.active.desc(), User.full_name)).all()


def open_orders(db: Session, u: User) -> list[tuple[WorkOrder, list[str]]]:
    """Незакрытые наряды сотрудника и в какой он там роли (слесарь / менеджер / запчастист)."""
    rows = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == u.branch_id, WorkOrder.status.in_(WO_ACTIVE),
                                              or_(*[getattr(WorkOrder, s) == u.id for s in SLOTS]))
                      .order_by(WorkOrder.created_at)).all()
    return [(o, [s for s in SLOTS if getattr(o, s) == u.id]) for o in rows]


def future_appointments(db: Session, u: User) -> list[Appointment]:
    return db.scalars(select(Appointment).where(Appointment.mechanic_id == u.id, Appointment.status == "booked",
                                                Appointment.start_at >= now()).order_by(Appointment.start_at)).all()


def candidates(db: Session, u: User) -> dict[str, list[User]]:
    """Кому можно передать: по каждой роли — работающие сотрудники этой роли (кроме увольняемого)."""
    from . import orders as osvc
    out = {}
    for slot, (role, _) in SLOTS.items():
        out[slot] = [x for x in osvc.staff_by_role(db, u.branch_id, role) if x.id != u.id]
    return out


def _uid(v) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def dismiss(db: Session, u: User, by: User, on: date, reason: str, form: dict) -> int:
    """Уволить. form: wo_<id>_<поле> = кому передать (пусто — оставить), appts_to = кому записи клиентов.
    Возвращает, сколько нарядов передано."""
    allowed = candidates(db, u)
    moved = 0
    for o, slots in open_orders(db, u):
        for slot in slots:
            new = _uid(form.get(f"wo_{o.id}_{slot}"))
            if not new or new not in {x.id for x in allowed[slot]}:
                continue
            setattr(o, slot, new)
            if slot == "mechanic_id":
                # Невыполненные работы этого наряда — новому слесарю
                for w in db.scalars(select(WoWork).where(WoWork.work_order_id == o.id, WoWork.mechanic_id == u.id,
                                                         WoWork.status != "done")):
                    w.mechanic_id = new
            moved += 1
    # Открытые таймеры работ закрываем, пост освобождаем
    for s in db.scalars(select(WoWorkSession).where(WoWorkSession.mechanic_id == u.id, WoWorkSession.ended_at.is_(None))):
        s.ended_at = now()
    for lift in db.scalars(select(Lift).where(Lift.mechanic_id == u.id)):
        lift.mechanic_id = None
    to = _uid(form.get("appts_to"))
    to = to if to in {x.id for x in allowed["mechanic_id"]} else None
    for a in future_appointments(db, u):
        a.mechanic_id = to
    u.active, u.dismissed_on, u.dismiss_reason, u.dismissed_by = False, on, reason.strip()[:1000], by.id
    return moved


def rehire(db: Session, u: User, on: date):
    """Вернуть на работу: снова может входить. Прошлое увольнение остаётся в журнале."""
    u.active, u.hired_on, u.dismissed_on, u.dismiss_reason, u.dismissed_by = True, on, None, "", None


def tenure(d1: date, d2: date) -> str:
    """Сколько проработал: «1 г. 8 мес.», «3 мес.», «12 дн.»."""
    months = (d2.year - d1.year) * 12 + d2.month - d1.month - (d2.day < d1.day)
    if months <= 0:
        return f"{max((d2 - d1).days, 0) + 1} дн."
    y, m = divmod(months, 12)
    return " ".join(p for p in (f"{y} г." if y else "", f"{m} мес." if m else "") if p)
