"""Табель: отметки по картам (пришёл/ушёл), часы за день и месяц, кто сейчас на месте.

Каждое «пик» на проходной — запись AttendanceEvent. Направление чередуется: если последняя отметка
сотрудника за сегодня «пришёл» — следующая будет «ушёл», и наоборот.
Часы считаются парами «пришёл → ушёл». Нет отметки ухода: сегодня — считаем до текущего момента,
в прошлые дни — день помечается «нет отметки ухода» и пара не считается (админ поправит вручную).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import AccessPoint, AttendanceEvent, StaffCard, User


def local_tz():
    return datetime.now().astimezone().tzinfo


def day_bounds(d: date):
    """Начало и конец местного дня — в UTC (в базе время хранится в UTC)."""
    from datetime import timezone
    start = datetime.combine(d, time.min, tzinfo=local_tz())
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def events_between(db: Session, branch_id, start: datetime, end: datetime, user_id=None):
    q = select(AttendanceEvent).where(AttendanceEvent.branch_id == branch_id, AttendanceEvent.at >= start,
                                      AttendanceEvent.at < end)
    if user_id:
        q = q.where(AttendanceEvent.user_id == user_id)
    return db.scalars(q.order_by(AttendanceEvent.at)).all()


def last_today(db: Session, user_id) -> AttendanceEvent | None:
    start, end = day_bounds(date.today())
    return db.scalars(select(AttendanceEvent).where(AttendanceEvent.user_id == user_id, AttendanceEvent.at >= start,
                                                    AttendanceEvent.at < end).order_by(AttendanceEvent.at.desc())).first()


def tap(db: Session, branch_id, uid: str, point: AccessPoint | None):
    """Пик картой на проходной. Возвращает (сотрудник, отметка) или текст ошибки."""
    card = db.scalar(select(StaffCard).where(StaffCard.uid == uid.strip(), StaffCard.active.is_(True)))
    user = db.get(User, card.user_id) if card else None
    if not user or not user.active or user.branch_id != branch_id:
        return "Карта не найдена или заблокирована"
    last = last_today(db, user.id)
    # Защита от двойного пика (держал карту у считывателя): повтор в течение 30 секунд игнорируем
    if last and (now() - _aware(last.at)).total_seconds() < 30:
        return user, last
    direction = "out" if last and last.direction == "in" else "in"
    ev = AttendanceEvent(user_id=user.id, card_id=card.id, point_id=point.id if point else None, direction=direction,
                         at=now(), method="card")
    db.add(ev)
    db.flush()
    return user, ev


def _aware(dt: datetime) -> datetime:
    """SQLite возвращает время без зоны — считаем, что оно в UTC (так мы его пишем)."""
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Day:
    minutes: int = 0
    first_in: datetime | None = None
    last_out: datetime | None = None
    open: bool = False          # пришёл и ещё не ушёл (сегодня) / забыл отметить уход (прошлые дни)
    events: list = field(default_factory=list)

    @property
    def hours(self) -> float:
        return round(self.minutes / 60, 1)


def compute_days(events, today: date | None = None) -> dict[date, Day]:
    """events одного сотрудника по возрастанию времени → {дата: Day}."""
    today = today or date.today()
    tz = local_tz()
    days: dict[date, Day] = {}
    for e in events:
        at = _aware(e.at).astimezone(tz)
        d = days.setdefault(at.date(), Day())
        d.events.append(e)
    for dte, d in days.items():
        start = None
        for e in d.events:
            at = _aware(e.at).astimezone(tz)
            if e.direction == "in":
                d.first_in = d.first_in or at
                if start is None:
                    start = at
            elif e.direction == "out" and start is not None:
                d.minutes += int((at - start).total_seconds() // 60)
                d.last_out = at
                start = None
        if start is not None:
            d.open = True
            if dte == today:
                d.minutes += int((datetime.now(tz) - start).total_seconds() // 60)
    return days


def month_grid(db: Session, branch_id, users, year: int, month: int):
    """{user_id: {день_месяца: Day}} + итоги."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    start, _ = day_bounds(first)
    _, end = day_bounds(last)
    by_user = {u.id: [] for u in users}
    for e in events_between(db, branch_id, start, end):
        if e.user_id in by_user:
            by_user[e.user_id].append(e)
    out = {}
    for uid, evs in by_user.items():
        days = compute_days(evs)
        out[uid] = {d.day: v for d, v in days.items() if d.month == month}
    return out


def days_present(db: Session, branch_id, user_id, year: int, month: int) -> int:
    grid = month_grid(db, branch_id, [type("U", (), {"id": user_id})()], year, month)
    return sum(1 for d in grid[user_id].values() if d.minutes > 0 or d.open)


def on_site_now(db: Session, branch_id, users) -> dict:
    """{user_id: последняя отметка за сегодня} — «на месте», если она «пришёл»."""
    start, end = day_bounds(date.today())
    last = {}
    for e in events_between(db, branch_id, start, end):
        last[e.user_id] = e
    return {u.id: last.get(u.id) for u in users}
