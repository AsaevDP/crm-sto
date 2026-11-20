"""«Зарплата сотрудников» для зама (20.11.2026, брат).

Бухгалтер видит всех всегда. Директору раздел открывает админ (галочка «Зарплата сотрудников» в правах).
Зам просит доступ → директор (право payroll.grant) разрешает на 1 день / неделю / месяц или отказывает,
может закрыть досрочно. Пока доступ действует, у зама есть «видеть всех» (web.get_ctx добавляет payroll.view_all),
а утверждать он может своим правом payroll.approve."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Notification, User
from ..models.payroll import PAYROLL_ACCESS_HOURS, PayrollAccess


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def active_until(db: Session, user_id):
    a = db.scalar(select(PayrollAccess).where(PayrollAccess.user_id == user_id, PayrollAccess.status == "approved")
                  .order_by(PayrollAccess.until.desc()))
    if a and a.until and _aware(a.until) > datetime.now(timezone.utc):
        return _aware(a.until)
    return None


def pending(db: Session, user_id) -> PayrollAccess | None:
    return db.scalar(select(PayrollAccess).where(PayrollAccess.user_id == user_id, PayrollAccess.status == "pending"))


def granters(db: Session, branch_id) -> list:
    """Кто решает: у кого право payroll.grant (директор; админ — всё)."""
    from ..permissions import effective_permissions
    return [u.id for u in db.scalars(select(User).where(User.branch_id == branch_id, User.active.is_(True)))
            if "payroll.grant" in effective_permissions(db, u)]


def request(db: Session, user: User, reason: str) -> str:
    from . import notify as nsvc
    if not reason.strip():
        return "Напишите, зачем нужен доступ (например: директор в отпуске, нужно утвердить неделю)"
    if pending(db, user.id):
        return "Запрос уже отправлен — ждём ответа директора"
    db.add(PayrollAccess(user_id=user.id, reason=reason.strip()[:300]))
    nsvc.send(db, granters(db, user.branch_id), "payroll_req", f"{user.full_name} просит доступ к «Зарплате сотрудников»",
              reason.strip()[:300], "/access-requests#payroll", skip=user.id)
    return ""


def decide(db: Session, a: PayrollAccess, by: User, action: str, hours: int = 0) -> str:
    from . import notify as nsvc
    if action == "close":
        if a.status != "approved":
            return "Доступ уже не действует"
        a.status, a.until = "closed", now()
        nsvc.send(db, [a.user_id], "payroll_no", "Доступ к «Зарплате сотрудников» закрыт", f"Закрыл(а): {by.full_name}",
                  "/payroll/weeks")
        return ""
    if a.status != "pending":
        return "По этому запросу уже решили"
    if action == "approve":
        if hours not in PAYROLL_ACCESS_HOURS:
            return "Выберите срок: 1 день, неделя или месяц"
        a.status, a.hours, a.until = "approved", hours, now() + timedelta(hours=hours)
        a.decided_by, a.decided_at = by.id, now()
        until = _aware(a.until).astimezone().strftime("%d.%m %H:%M")
        nsvc.send(db, [a.user_id], "payroll_ok", f"«Зарплата сотрудников» открыта — до {until}",
                  f"Разрешил(а): {by.full_name} · на {PAYROLL_ACCESS_HOURS[hours]}", "/payroll/weeks")
    elif action == "decline":
        a.status, a.decided_by, a.decided_at = "declined", by.id, now()
        nsvc.send(db, [a.user_id], "payroll_no", "Доступ к «Зарплате сотрудников» — отказано",
                  f"Ответил(а): {by.full_name}", "/payroll/weeks")
    else:
        return "Неизвестное действие"
    for n in db.scalars(select(Notification).where(Notification.kind == "payroll_req", Notification.read_at.is_(None))):
        n.read_at = now()   # окошко «зам просит» у остальных решающих больше не нужно
    return ""


def lists(db: Session, branch_id) -> dict:
    rows = db.scalars(select(PayrollAccess).where(PayrollAccess.branch_id == branch_id)
                      .order_by(PayrollAccess.created_at.desc()).limit(30)).all()
    t = datetime.now(timezone.utc)
    act = [a for a in rows if a.status == "approved" and a.until and _aware(a.until) > t]
    return {"pay_pending": [a for a in rows if a.status == "pending"], "pay_active": act,
            "pay_history": [a for a in rows if a.status != "pending" and a not in act][:10], "PAY_HOURS": PAYROLL_ACCESS_HOURS}
