"""Опоздания, неявки, ранний уход, задержка после работы (24.11.2026, брат).

Считается на лету из отметок «пришёл / ушёл»; в базе — только «Принял к сведению» (AttendanceAck), решения
руководства (AttendanceDecision) и «Отпросился заранее» (AttendanceLeave). Начало и конец дня — «Запись»
(schedule.day_start / day_end, 8:00–19:00, обед входит — 11 ч), выходные — schedule.days_off, пороги — «Настройки → Табель».

- Опоздание (late): первая отметка «пришёл» с 8:15 до 8:59. Неявка (absent): с 9:00 или вообще без отметки.
- Ранний уход (early): последняя отметка «ушёл» раньше конца дня на 15 мин и больше (день уже закончился).
- Задержка (overtime): ушёл позже конца дня на 30–59 мин — 🟣 руководству, на час и больше — 🔴 «решить про премию».
  Ставка за день больше не становится: премию назначают вручную, сотрудник пишет, что делал.
- Не отметил уход (no_out): день закончился на «пришёл» (сегодня — через час после конца дня).
- Деньги — только тем, кто на окладе: удерживается каждый начатый час (8:20 → 1 ч, 9:05 → 2 ч, ушёл в 18:10 → 1 ч).
  Цена часа = ставка за день ÷ часов в дне (оклад в месяц: ÷ рабочих дней месяца, потом ÷ часов).
- Решают директор, зам, админ (attendance.decide): «Отпросился заранее», «Уважительная причина» — без удержания и не
  нарушение; «Удержать за время», «Удержать + штраф» — нарушение. Штраф (500 ₴) предлагается с 3-го нарушения за 30 дней.
  Бухгалтер видит решения и комментарии; пока не решено — сотрудника в неделе не отметить «проверено».
Проверяем всех, кроме админа (владельца). Выходные — опоздание, неявка, ранний уход, задержка не считаются.
"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import AttendanceAck, AttendanceDecision, AttendanceLeave, PayrollAccrual, Role, User
from ..models.staff import ATT_DECISIONS, ATT_REASONS
from . import settings as st
from .attendance import _aware, day_bounds, events_between, local_tz

LEADERS = ("director", "deputy")
KIND_NAME = {"late": "Опоздание", "absent": "Неявка", "early": "Ранний уход", "overtime": "Задержался после работы",
             "no_out": "Не отметил уход"}
MONEY_KINDS = ("late", "absent", "early")          # за них удерживаем время (окладникам)
DECIDE_KINDS = ("late", "absent", "early", "overtime")
VIOLATION = ("deduct", "fine")


@dataclass
class Flag:
    user: User
    day: date
    kind: str
    minutes: int = 0                  # опоздание / ранний уход / задержка, мин
    hours: int = 0                    # сколько часов удержать (целые часы)
    first_in: datetime | None = None
    last_in: datetime | None = None
    last_out: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    level: str = ""                   # overtime: info / red
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.user.id}|{self.day.isoformat()}|{self.kind}"

    def text(self) -> str:
        d = self.day.strftime("%d.%m")
        if self.kind == "late":
            return f"{d}: пришли в {self.first_in:%H:%M}, начало в {self.start:%H:%M} — опоздание {fmt_min(self.minutes)}"
        if self.kind == "absent":
            if self.first_in:
                return f"{d}: отметка прихода только в {self.first_in:%H:%M} (с {self.extra['absent_at']:%H:%M} — неявка)"
            return f"{d}: нет отметки прихода"
        if self.kind == "early":
            return f"{d}: ушли в {self.last_out:%H:%M}, конец дня в {self.end:%H:%M} — раньше на {fmt_min(self.minutes)}"
        if self.kind == "overtime":
            return f"{d}: ушли в {self.last_out:%H:%M}, конец дня в {self.end:%H:%M} — задержка {fmt_min(self.minutes)}"
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
    i = lambda k, d: int(at.get(k, d) or d)
    return {"start_h": int(sc.get("day_start", 8)), "end_h": int(sc.get("day_end", 19)),
            "days_off": set(sc.get("days_off", [5, 6])), "late": i("late_min", 15), "absent": i("absent_min", 60),
            "no_out": i("no_out_after_min", 60), "over_info": i("over_info_min", 30), "over_red": i("over_red_min", 60),
            "fine": Decimal(str(at.get("fine_amount", 500) or 0)), "fine_from": i("fine_from", 3), "from": start_from}


def checked_users(db: Session, bid) -> list[User]:
    """Кого проверяем: все работающие, кроме админа (владельца)."""
    return list(db.scalars(select(User).join(Role, Role.id == User.role_id).where(
        User.branch_id == bid, User.active.is_(True), Role.code != "admin")))


def _hm(s: str) -> time:
    try:
        h, m = (int(x) for x in (s or "").split(":")[:2])
        return time(max(0, min(23, h)), max(0, min(59, m)))
    except ValueError:
        return time(0, 0)


def leaves(db: Session, bid, d_from: date, d_to: date) -> dict:
    """{(user_id, день): [AttendanceLeave]}."""
    out = {}
    for lv in db.scalars(select(AttendanceLeave).where(AttendanceLeave.branch_id == bid, AttendanceLeave.day >= d_from,
                                                       AttendanceLeave.day <= d_to)):
        out.setdefault((lv.user_id, lv.day), []).append(lv)
    return out


def _covered(lvs, d: date, tz, a: datetime, b: datetime) -> bool:
    """Отпросился на этот отрезок времени (с запасом в 15 мин)?"""
    for lv in lvs or []:
        f = datetime.combine(d, _hm(lv.t_from), tzinfo=tz) - timedelta(minutes=15)
        t = datetime.combine(d, _hm(lv.t_to), tzinfo=tz) + timedelta(minutes=15)
        if f <= a and b <= t:
            return True
    return False


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
    lvs = leaves(db, bid, d_from, today)
    out: list[Flag] = []
    for u in users:
        hired = u.hired_date
        d = d_from
        while d <= today:
            if d < hired:
                d += timedelta(days=1)
                continue
            day_ev = evs.get((u.id, d), [])
            lv = lvs.get((u.id, d))
            start = datetime.combine(d, time(p["start_h"]), tzinfo=tz)
            end = datetime.combine(d, time(min(p["end_h"], 23)), tzinfo=tz)
            absent_at = start + timedelta(minutes=p["absent"])
            ins = [_aware(e.at).astimezone(tz) for e in day_ev if e.direction == "in"]
            first_in = min(ins) if ins else None
            last = day_ev[-1] if day_ev else None
            last_out = _aware(last.at).astimezone(tz) if last is not None and last.direction == "out" else None
            workday = d.weekday() not in p["days_off"]
            base = dict(first_in=first_in, start=start, end=end)
            if workday:
                late_m = int((first_in - start).total_seconds() // 60) if first_in else None
                if first_in and first_in < absent_at:
                    if late_m >= p["late"] and not _covered(lv, d, tz, start, first_in):
                        out.append(Flag(u, d, "late", late_m, math.ceil(late_m / 60), **base))
                elif now_ >= absent_at and not _covered(lv, d, tz, start, first_in or end):
                    hrs = math.ceil(late_m / 60) if first_in else 0
                    out.append(Flag(u, d, "absent", late_m or 0, hrs, extra={"absent_at": absent_at}, **base))
                if last_out and (d < today or now_ >= end):
                    early_m = int((end - last_out).total_seconds() // 60)
                    over_m = -early_m
                    if early_m >= p["late"] and not _covered(lv, d, tz, last_out, end):
                        out.append(Flag(u, d, "early", early_m, math.ceil(early_m / 60), last_out=last_out, **base))
                    elif over_m >= p["over_info"]:
                        out.append(Flag(u, d, "overtime", over_m, 0, last_out=last_out,
                                        level="red" if over_m >= p["over_red"] else "info", **base))
            if last is not None and last.direction == "in":
                last_in = _aware(last.at).astimezone(tz)
                if d < today or now_ >= end + timedelta(minutes=p["no_out"]):
                    out.append(Flag(u, d, "no_out", 0, 0, last_in=last_in, **base))
            d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- деньги
def salary_line(db: Session, u: User):
    """Строка «Оклад» схемы сотрудника (или None — не на окладе, деньги за время не трогаем)."""
    from . import payroll as psvc
    return next((l for l in psvc.lines_for(db, u) if l.kind == "salary" and (l.rate or 0) > 0), None)


def hour_rate(db: Session, bid, u: User, day: date) -> Decimal | None:
    line = salary_line(db, u)
    if not line:
        return None
    p = params(db, bid)
    hours = max(1, p["end_h"] - p["start_h"])
    day_rate = Decimal(line.rate)
    if line.salary_period == "month":
        import calendar
        n = calendar.monthrange(day.year, day.month)[1]
        work = sum(1 for k in range(1, n + 1) if date(day.year, day.month, k).weekday() not in p["days_off"]) or 1
        day_rate = day_rate / work
    return (day_rate / hours).quantize(Decimal("0.01"))


def hours_to_hold(db: Session, bid, f: Flag) -> int:
    """Неявка без единой отметки: окладу «в день» день и так не оплачен — 0; окладу «в месяц» — весь день."""
    if f.kind == "absent" and not f.first_in:
        line = salary_line(db, f.user)
        p = params(db, bid)
        return (p["end_h"] - p["start_h"]) if line and line.salary_period == "month" else 0
    return f.hours


# ---------------------------------------------------------------- решения
def decisions(db: Session, bid, since: date) -> dict:
    return {(x.user_id, x.day, x.kind): x for x in db.scalars(select(AttendanceDecision).where(
        AttendanceDecision.branch_id == bid, AttendanceDecision.day >= since))}


def acks(db: Session, bid, since: date) -> set:
    return {(a.user_id, a.day, a.kind, a.side) for a in db.scalars(select(AttendanceAck).where(
        AttendanceAck.branch_id == bid, AttendanceAck.day >= since))}


def _since(db, bid):
    return max(params(db, bid)["from"], date.today() - timedelta(days=40))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def violations(db: Session, bid, uid, day: date) -> int:
    """Нарушений (решено «удержать» / «штраф») с 30 дней до этого дня и позже (решают не всегда по порядку)."""
    return len([x for x in db.scalars(select(AttendanceDecision).where(
        AttendanceDecision.branch_id == bid, AttendanceDecision.user_id == uid, AttendanceDecision.decision.in_(VIOLATION),
        AttendanceDecision.day > day - timedelta(days=30)))
        if x.kind in MONEY_KINDS])


def can_decide(ctx_user, target: User) -> bool:
    """О себе руководитель не решает; опоздание директора / зама решает другой руководитель или админ."""
    return ctx_user.id != target.id


def for_user(db: Session, bid, user: User, now_: datetime | None = None) -> dict:
    """Своё. new — опоздания без «Принял к сведению» (🔴 крупный), week — принятые за эту неделю (🟠), warn — ⚠
    (не отметил уход; неявка, пока руководство не решило), over — задержался, а «что делали» не написал."""
    empty = {"new": [], "week": [], "warn": [], "over": [], "days": {}}
    if user.role and user.role.code == "admin":
        return empty
    fl = flags(db, bid, [user], now_)
    since = _since(db, bid)
    ak, dec = acks(db, bid, since), decisions(db, bid, since)
    ws = week_start(date.today())
    new = [f for f in fl if f.kind == "late" and (user.id, f.day, "late", "self") not in ak]
    week = [f for f in fl if f.kind == "late" and f.day >= ws and (user.id, f.day, "late", "self") in ak]
    warn = [f for f in fl if f.kind == "no_out" or (f.kind == "absent" and not (dec.get((user.id, f.day, "absent"))
                                                                               and dec[(user.id, f.day, "absent")].decision))]
    over = [f for f in fl if f.kind == "overtime" and not (dec.get((user.id, f.day, "overtime"))
                                                         and (dec[(user.id, f.day, "overtime")].note
                                                              or dec[(user.id, f.day, "overtime")].decision))]
    days = {}
    for f in fl:
        days.setdefault(f.day, []).append({"f": f, "d": dec.get((user.id, f.day, f.kind))})
    return {"new": sorted(new, key=lambda f: f.day), "week": week, "warn": sorted(warn, key=lambda f: f.day),
            "over": over, "days": days, "dec": dec}


def for_boss(db: Session, bid, me: User, now_: datetime | None = None) -> list[dict]:
    """Руководству — что ждёт решения: опоздание, неявка, ранний уход, задержка (кнопки — директор, зам, админ);
    не отметил уход («Принял к сведению», «Вписать время»). Решённое и принятое уходит."""
    since = _since(db, bid)
    ak, dec = acks(db, bid, since), decisions(db, bid, since)
    p = params(db, bid)
    out = []
    for f in flags(db, bid, None, now_):
        if f.user.id == me.id:
            continue
        if f.kind == "no_out":
            if (f.user.id, f.day, "no_out", "boss") in ak:
                continue
            out.append({"f": f, "d": None, "level": "red"})
            continue
        d = dec.get((f.user.id, f.day, f.kind))
        if d and d.decision:
            continue
        x = {"f": f, "d": d, "level": f.level or "red"}
        if f.kind in MONEY_KINDS:
            rate = hour_rate(db, bid, f.user, f.day)
            hrs = hours_to_hold(db, bid, f)
            n = violations(db, bid, f.user.id, f.day) + 1
            x.update(rate=rate, hrs=hrs, amount=(rate * hrs).quantize(Decimal("0.01")) if rate is not None else None,
                     nth=n, fine_due=n >= p["fine_from"], fine=p["fine"])
        out.append(x)
    order = {"red": 0, "info": 1}
    return sorted(out, key=lambda x: (order.get(x["level"], 0), -x["f"].day.toordinal(), x["f"].user.full_name))


def undecided(db: Session, bid, uid, d_from: date, d_to: date) -> list[Flag]:
    """Что за эти дни у сотрудника ещё не решено (опоздание, неявка, ранний уход, задержка от часа) — бухгалтер не
    может отметить его неделю «проверено»."""
    u = db.get(User, uid)
    if not u or (u.role and u.role.code == "admin"):
        return []
    dec = decisions(db, bid, d_from)
    return [f for f in flags(db, bid, [u]) if d_from <= f.day <= d_to and f.kind in DECIDE_KINDS
            and (f.kind != "overtime" or f.level == "red")
            and not (dec.get((uid, f.day, f.kind)) and dec[(uid, f.day, f.kind)].decision)]


def _cancel(db: Session, aid, why: str):
    if aid:
        a = db.get(PayrollAccrual, aid)
        if a and a.status != "cancelled":
            a.status = "cancelled"
            a.note = f"{a.note} · отменено: {why}"[:300]


def decide(db: Session, bid, me: User, u: User, day: date, kind: str, decision: str, reason: str = "",
           comment: str = "", fine: Decimal | None = None, bonus: Decimal | None = None) -> str:
    """Решение руководителя. Ошибка или ''. Поменяли решение — прежние удержание / штраф / премия отменяются."""
    if kind not in DECIDE_KINDS or decision not in ATT_DECISIONS:
        return "Неизвестное решение"
    if (kind == "overtime") != (decision in ("bonus", "nobonus")):
        return "Это решение сюда не подходит"
    if decision == "excused" and reason not in ATT_REASONS:
        return "Выберите уважительную причину"
    if decision == "excused" and reason == "other" and not comment.strip():
        return "Напишите, что случилось"
    f = next((x for x in flags(db, bid, [u]) if x.day == day and x.kind == kind), None)
    if not f:
        return "За этот день такого нет"
    x = db.scalar(select(AttendanceDecision).where(AttendanceDecision.branch_id == bid, AttendanceDecision.user_id == u.id,
                                                   AttendanceDecision.day == day, AttendanceDecision.kind == kind))
    if x is None:
        x = AttendanceDecision(user_id=u.id, day=day, kind=kind)
        db.add(x)
    for k in ("deduct_id", "fine_id", "bonus_id"):
        _cancel(db, getattr(x, k), f"решение изменил {me.short_name}")
        setattr(x, k, None)
    x.decision, x.reason, x.comment = decision, reason if decision == "excused" else "", comment.strip()[:500]
    x.decided_by, x.decided_at, x.hours = me.id, now(), 0
    d = day.strftime("%d.%m")
    if decision in VIOLATION:
        rate = hour_rate(db, bid, u, day)
        hrs = hours_to_hold(db, bid, f)
        if rate is not None and hrs > 0:
            amt = (rate * hrs).quantize(Decimal("0.01"))
            a = PayrollAccrual(user_id=u.id, base_amount=Decimal(hrs), rate=rate, amount=-amt, on_date=day, kind="deduction",
                               note=f"{KIND_NAME[kind]} {d}: −{hrs} ч × {rate} ₴"[:300])
            db.add(a)
            db.flush()
            x.deduct_id, x.hours = a.id, hrs
        if decision == "fine":
            fa = Decimal(fine if fine is not None else params(db, bid)["fine"]).quantize(Decimal("0.01"))
            if fa > 0:
                n = violations(db, bid, u.id, day)   # уже вместе с этим решением
                a = PayrollAccrual(user_id=u.id, base_amount=fa, rate=0, amount=-fa, on_date=day, kind="deduction",
                                   note=f"Штраф: {KIND_NAME[kind].lower()} {d} — {n}-е нарушение за 30 дней"[:300])
                db.add(a)
                db.flush()
                x.fine_id = a.id
    elif decision == "bonus":
        ba = Decimal(bonus or 0).quantize(Decimal("0.01"))
        if ba <= 0:
            return "Впишите сумму премии"
        a = PayrollAccrual(user_id=u.id, base_amount=ba, rate=0, amount=ba, on_date=day, kind="adjust",
                           note=f"Премия: задержался {d} до {f.last_out:%H:%M}" + (f" — {comment.strip()}" if comment.strip() else ""))
        a.note = a.note[:300]
        db.add(a)
        db.flush()
        x.bonus_id = a.id
    db.flush()
    return ""


def set_note(db: Session, u: User, day: date, text: str) -> str:
    """Сотрудник: «что делали после работы» (задержка)."""
    if not text.strip():
        return "Напишите, что делали"
    x = db.scalar(select(AttendanceDecision).where(AttendanceDecision.branch_id == u.branch_id, AttendanceDecision.user_id == u.id,
                                                   AttendanceDecision.day == day, AttendanceDecision.kind == "overtime"))
    if x is None:
        x = AttendanceDecision(user_id=u.id, day=day, kind="overtime")
        db.add(x)
    x.note = text.strip()[:1000]
    db.flush()
    return ""


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


def closed_orders_after(db: Session, u: User, f: Flag) -> list:
    """Что сотрудник закрыл в нарядах после конца дня (чтобы видеть, правда ли работал)."""
    from ..models import WoWork, WorkOrder
    if not f.end or not f.last_out:
        return []
    rows = db.execute(select(WoWork, WorkOrder).join(WorkOrder, WorkOrder.id == WoWork.work_order_id).where(
        WoWork.mechanic_id == u.id, WoWork.done_at >= f.end, WoWork.done_at <= f.last_out + timedelta(minutes=5))).all()
    return [(w, o) for w, o in rows]
