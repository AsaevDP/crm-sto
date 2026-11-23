"""Опоздания и пропущенные отметки табеля (24.11.2026, брат).

Считается на лету из отметок «пришёл / ушёл» (attendance_events); в базе хранится только «Принял к сведению»
(AttendanceAck). Начало дня — «Запись» (schedule.day_start, по умолчанию 8:00), выходные — schedule.days_off.

- Опоздание (late): первая отметка «пришёл» на 30 мин и позже начала дня (но не позже начала + 3 ч).
  Сотруднику — крупный 🔴 у «Табеля» и окошко «Опаздывать нельзя» → «Принял к сведению» → 🟠 «опозданий за неделю»
  (пн–вс). Директор / зам опоздал — ещё и админу 🔴 (принять к сведению).
- Не пришёл (absent): к началу дня + 3 ч (11:00) нет отметки «пришёл». Руководству 🔴, сотруднику ⚠ «если были
  на работе — сообщите бухгалтеру или директору» (гаснет, когда вписали приход вручную или руководство приняло).
- Не отметил уход (no_out): день закончился с «пришёл» без «ушёл» (сегодня — через час после конца дня).
  Сотруднику ⚠ до тех пор, пока бухгалтер / директор / зам / админ не впишут время ухода; руководству 🔴 до
  «Принял к сведению» (одного из них достаточно).
Проверяем всех, кроме админа (владельца). Выходные — опоздание и прогул не считаются.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AttendanceAck, Role, User
from . import settings as st
from .attendance import _aware, day_bounds, events_between, local_tz

LEADERS = ("director", "deputy")      # их опоздание видит и админ
KIND_NAME = {"late": "Опоздание", "absent": "Не пришёл", "no_out": "Не отметил уход"}


@dataclass
class Flag:
    user: User
    day: date
    kind: str                       # late / absent / no_out
    minutes: int = 0                # опоздание, мин
    first_in: datetime | None = None
    last_in: datetime | None = None
    start: datetime | None = None   # начало рабочего дня (местное)

    @property
    def key(self):
        return f"{self.user.id}|{self.day.isoformat()}|{self.kind}"

    def text(self) -> str:
        d = self.day.strftime("%d.%m")
        if self.kind == "late":
            return (f"{d}: пришли в {self.first_in:%H:%M}, начало в {self.start:%H:%M} — опоздание {fmt_min(self.minutes)}")
        if self.kind == "absent":
            if self.first_in:
                return f"{d}: отметка прихода только в {self.first_in:%H:%M} — позже {self.start + timedelta(hours=3):%H:%M} это неявка"
            return f"{d}: нет отметки прихода"
        return f"{d}: пришли в {self.last_in:%H:%M}, отметки ухода нет"


def fmt_min(m: int) -> str:
    h, mm = divmod(int(m), 60)
    return (f"{h} ч {mm} мин" if mm else f"{h} ч") if h else f"{mm} мин"


def params(db: Session, bid) -> dict:
    sc, at = st.get(db, bid, "schedule"), st.get(db, bid, "attendance")
    try:
        start_from = date.fromisoformat(at.get("alerts_from") or "")
    except ValueError:
        start_from = date.today()
    return {"start_h": int(sc.get("day_start", 8)), "end_h": int(sc.get("day_end", 19)),
            "days_off": set(sc.get("days_off", [5, 6])), "late": int(at.get("late_min", 30)),
            "absent_h": float(at.get("absent_after_h", 3)), "no_out": int(at.get("no_out_after_min", 60)),
            "from": start_from}


def checked_users(db: Session, bid) -> list[User]:
    """Кого проверяем: все работающие, кроме админа (владельца)."""
    return list(db.scalars(select(User).join(Role, Role.id == User.role_id).where(
        User.branch_id == bid, User.active.is_(True), Role.code != "admin")))


def flags(db: Session, bid, users=None, now_: datetime | None = None, back_days: int = 35) -> list[Flag]:
    tz = local_tz()
    now_ = (now_ or datetime.now(tz)).astimezone(tz)
    today = now_.date()
    p = params(db, bid)
    d_from = max(p["from"], today - timedelta(days=back_days))
    if d_from > today:
        return []
    users = checked_users(db, bid) if users is None else users
    by_id = {u.id: u for u in users}
    evs: dict = {}
    start_utc, _ = day_bounds(d_from)
    _, end_utc = day_bounds(today)
    for e in events_between(db, bid, start_utc, end_utc):
        if e.user_id in by_id:
            evs.setdefault((e.user_id, _aware(e.at).astimezone(tz).date()), []).append(e)
    out: list[Flag] = []
    for u in users:
        hired = u.hired_date
        d = d_from
        while d <= today:
            if d < hired:
                d += timedelta(days=1)
                continue
            day_ev = evs.get((u.id, d), [])
            start = datetime.combine(d, time(p["start_h"]), tzinfo=tz)
            cutoff = start + timedelta(hours=p["absent_h"])
            ins = [_aware(e.at).astimezone(tz) for e in day_ev if e.direction == "in"]
            first_in = min(ins) if ins else None
            workday = d.weekday() not in p["days_off"]
            if workday:
                if first_in and first_in <= cutoff:
                    late = int((first_in - start).total_seconds() // 60)
                    if late >= p["late"]:
                        out.append(Flag(u, d, "late", late, first_in, start=start))
                elif now_ >= cutoff:
                    out.append(Flag(u, d, "absent", 0, first_in, start=start))
            if day_ev and day_ev[-1].direction == "in":
                last_in = _aware(day_ev[-1].at).astimezone(tz)
                end = datetime.combine(d, time(min(p["end_h"], 23)), tzinfo=tz) + timedelta(minutes=p["no_out"])
                if d < today or now_ >= end:
                    out.append(Flag(u, d, "no_out", 0, first_in, last_in, start=start))
            d += timedelta(days=1)
    return out


def acks(db: Session, bid, since: date) -> set:
    return {(a.user_id, a.day, a.kind, a.side) for a in db.scalars(select(AttendanceAck).where(
        AttendanceAck.branch_id == bid, AttendanceAck.day >= since))}


def _since(db, bid):
    return max(params(db, bid)["from"], date.today() - timedelta(days=40))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def for_user(db: Session, bid, user: User, now_: datetime | None = None) -> dict:
    """Своё: new — опоздания без «Принял к сведению» (🔴), week — принятые за эту неделю (🟠), warn — ⚠."""
    if user.role and user.role.code == "admin":
        return {"new": [], "week": [], "warn": []}
    fl = flags(db, bid, [user], now_)
    ak = acks(db, bid, _since(db, bid))
    ws = week_start(date.today())
    new = [f for f in fl if f.kind == "late" and (user.id, f.day, "late", "self") not in ak]
    week = [f for f in fl if f.kind == "late" and f.day >= ws and (user.id, f.day, "late", "self") in ak]
    warn = [f for f in fl if f.kind == "no_out"
            or (f.kind == "absent" and (user.id, f.day, "absent", "boss") not in ak)]
    return {"new": sorted(new, key=lambda f: f.day), "week": week, "warn": sorted(warn, key=lambda f: f.day)}


def for_boss(db: Session, bid, me: User, now_: datetime | None = None) -> list[Flag]:
    """Руководству (бухгалтер, директор, зам, админ): не пришёл и не отметил уход — у всех, кроме себя; админу ещё
    опоздания директора и зама. Без «Принял к сведению» одного из руководителей."""
    is_admin = me.role and me.role.code == "admin"
    ak = acks(db, bid, _since(db, bid))
    out = []
    for f in flags(db, bid, None, now_):
        if f.user.id == me.id or (f.user.id, f.day, f.kind, "boss") in ak:
            continue
        if f.kind in ("absent", "no_out") or (f.kind == "late" and is_admin and f.user.role
                                               and f.user.role.code in LEADERS):
            out.append(f)
    return sorted(out, key=lambda f: (f.day, f.user.full_name), reverse=True)


def ack(db: Session, bid, me: User, user_id, day: date, kind: str, side: str) -> bool:
    if kind not in KIND_NAME or side not in ("self", "boss"):
        return False
    if db.scalar(select(AttendanceAck.id).where(AttendanceAck.branch_id == bid, AttendanceAck.user_id == user_id,
                                                AttendanceAck.day == day, AttendanceAck.kind == kind,
                                                AttendanceAck.side == side)):
        return True
    db.add(AttendanceAck(user_id=user_id, day=day, kind=kind, side=side, acked_by=me.id))
    db.flush()
    return True
