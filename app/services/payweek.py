"""Недельная зарплата (11.11.2026).

Неделя — с понедельника по воскресенье. Работа попадает в неделю по дню «выполнено» (слесарь отметил),
начисление появляется, когда наряд закрыт (оплачен). Выполненные работы по незакрытым нарядам видны отдельно
как «ждут закрытия наряда» — с прикидкой суммы, чтобы бухгалтер понимал, что ещё придёт.

Путь недели: идёт → проверена бухгалтером (payroll.check) → утверждена (payroll.edit — админ или кому он дал) →
выдача в понедельник (payroll.pay): по каждому сотруднику отметка «выдано», способ и карта/счёт.
Исправлять начисления, добавлять премии и штрафы — только payroll.edit. Всё пишется в журнал (audit_log).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PayrollAccrual, PayrollPayout, PayWeek, User, WoWork, WorkOrder
from . import payroll as psvc

D0 = Decimal(0)
COLS = {"works": "Работы", "parts": "Запчасти", "salary": "Оклад", "other": "Премии и прочее", "deduction": "Удержания"}


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def pay_day(start: date) -> date:
    """День выплаты — понедельник следующей недели."""
    return start + timedelta(days=7)


def get_week(db: Session, branch_id, start: date, create: bool = False) -> PayWeek | None:
    w = db.scalar(select(PayWeek).where(PayWeek.branch_id == branch_id, PayWeek.week_start == start))
    if w is None and create:
        w = PayWeek(branch_id=branch_id, week_start=start, status="open")
        db.add(w)
        db.flush()
    return w


def _pending(db: Session, users, start: date, end: date) -> dict:
    """Выполненные за неделю работы, по которым ещё нет начисления (наряд не закрыт): {uid: [(work, wo, сумма)]}."""
    from .attendance import day_bounds
    t1, t2 = day_bounds(start)[0], day_bounds(end)[0] + timedelta(days=1)
    ids = [u.id for u in users]
    accrued = set(db.scalars(select(PayrollAccrual.wo_work_id).where(PayrollAccrual.wo_work_id.is_not(None),
                                                                     PayrollAccrual.status != "cancelled")))
    out = {}
    by_id = {u.id: u for u in users}
    for w in db.scalars(select(WoWork).where(WoWork.status == "done", WoWork.done_at >= t1, WoWork.done_at < t2)):
        wo = db.get(WorkOrder, w.work_order_id)
        uid = w.mechanic_id or (wo.mechanic_id if wo else None)
        if uid not in ids or w.id in accrued or not wo or wo.status == "cancel":
            continue
        u = by_id[uid]
        line = psvc._best(psvc.lines_for(db, u) or psvc.default_lines(db, u), {"works"}, None, set())
        est = D0
        if line:
            base = Decimal(w.price or 0)
            est = psvc.money(base * line.rate / 100) if line.kind == "percent" else psvc.money(line.rate * Decimal(w.qty or 1))
        out.setdefault(uid, []).append((w, wo, est))
    return out


def week_data(db: Session, branch_id, start: date, users) -> dict:
    """Ведомость недели: по каждому сотруднику колонки, начислено, выдано, к выдаче, наряды, ожидание."""
    end = start + timedelta(days=6)
    week = get_week(db, branch_id, start)
    ids = [u.id for u in users]
    rows = {uid: {**{k: D0 for k in COLS}, "accrued": D0, "paid": D0, "due": D0, "items": [], "payouts": [],
                  "orders": set(), "pending": [], "pending_sum": D0} for uid in ids}
    for a in psvc.accruals(db, ids, start, end):
        r = rows[a.user_id]
        col = psvc.column_of(a)
        r[col if col in COLS else "other"] += a.amount
        r["accrued"] += a.amount
        r["items"].append(a)
        if a.work_order_id:
            r["orders"].add(a.work_order_id)
    pays = db.scalars(select(PayrollPayout).where(PayrollPayout.user_id.in_(ids), PayrollPayout.week_id == week.id)
                      .order_by(PayrollPayout.created_at)).all() if (ids and week) else []
    for p in pays:
        rows[p.user_id]["paid"] += p.amount
        rows[p.user_id]["payouts"].append(p)
    for uid, lst in _pending(db, users, start, end).items():
        rows[uid]["pending"] = lst
        rows[uid]["pending_sum"] = sum((x[2] for x in lst), D0)
    for r in rows.values():
        r["due"] = r["accrued"] - r["paid"]
        r["items"].sort(key=lambda a: (a.on_date, a.created_at))
    tot = {k: sum((r[k] for r in rows.values()), D0) for k in (*COLS, "accrued", "paid", "due", "pending_sum")}
    tot["unpaid_people"] = sum(1 for r in rows.values() if r["due"] > 0)
    return {"week": week, "start": start, "end": end, "rows": rows, "tot": tot, "pay_day": pay_day(start)}


def history(db: Session, branch_id, users, before: date, n: int = 8) -> list[dict]:
    """Прошлые недели: начислено / выдано / статус — для списка внизу и «долга прошлых недель»."""
    out = []
    for k in range(1, n + 1):
        s = before - timedelta(days=7 * k)
        d = week_data(db, branch_id, s, users)
        out.append({"start": s, "end": d["end"], "week": d["week"], "accrued": d["tot"]["accrued"], "paid": d["tot"]["paid"],
                    "due": d["tot"]["due"]})
    return out


def set_status(db: Session, week: PayWeek, status: str, user: User):
    week.status = status
    if status == "checked":
        week.checked_by, week.checked_at = user.id, now()
    elif status == "approved":
        week.approved_by, week.approved_at = user.id, now()
    elif status == "open":
        week.checked_by = week.checked_at = week.approved_by = week.approved_at = None


def pay(db: Session, week: PayWeek, user: User, amount: Decimal, method: str, account: str, note: str = "") -> PayrollPayout:
    end = week.week_start + timedelta(days=6)
    p = PayrollPayout(user_id=user.id, period_from=week.week_start, period_to=end, amount=psvc.money(amount),
                      paid_on=date.today(), method=method, account=(account or "")[:80], note=note[:300], week_id=week.id)
    db.add(p)
    db.flush()
    return p


def refresh_paid(db: Session, week: PayWeek, users):
    """Всё выдано → неделя «выплачена»; появился долг (доначислили после выплаты) → снова «утверждена»."""
    d = week_data(db, week.branch_id, week.week_start, users)
    if week.status in ("approved", "paid"):
        week.status = "paid" if d["tot"]["accrued"] > 0 and d["tot"]["unpaid_people"] == 0 else "approved"


def days_present(db: Session, branch_id, user_id, start: date, end: date) -> int:
    """Сколько дней сотрудник был на месте (по табелю) с start по end."""
    from .attendance import compute_days, day_bounds, events_between
    t1, t2 = day_bounds(start)[0], day_bounds(end)[0] + timedelta(days=1)
    days = compute_days(events_between(db, branch_id, t1, t2, user_id=user_id))
    return sum(1 for d in days if start <= d <= end)


def accrue_week_salary(db: Session, branch_id, start: date, users) -> Decimal:
    """Оклад «за день» за неделю: ставка × дни по табелю. Повторно за ту же неделю не начисляет."""
    end = start + timedelta(days=6)
    total = D0
    for u in users:
        for l in psvc.lines_for(db, u):
            if l.kind != "salary" or l.salary_period != "day":
                continue
            exists = db.scalar(select(PayrollAccrual.id).where(PayrollAccrual.user_id == u.id, PayrollAccrual.kind == "salary",
                                                               PayrollAccrual.scheme_line_id == l.id,
                                                               PayrollAccrual.on_date >= start, PayrollAccrual.on_date <= end,
                                                               PayrollAccrual.status != "cancelled"))
            if exists:
                continue
            n = days_present(db, branch_id, u.id, start, end)
            if n <= 0:
                continue
            amount = psvc.money(l.rate * n)
            db.add(PayrollAccrual(user_id=u.id, scheme_line_id=l.id, base_amount=Decimal(n), rate=l.rate, amount=amount,
                                  on_date=end, kind="salary", note=f"Оклад за неделю {start:%d.%m}–{end:%d.%m}: {n} дн."))
            total += amount
    return total
