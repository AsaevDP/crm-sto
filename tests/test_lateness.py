"""Табель (24.11.2026, брат): опоздание с 8:15, неявка с 9:00, ранний уход, задержка после работы, «отпросился»,
решения директора и деньги окладникам — целыми часами (11-часовой день 8:00–19:00, обед входит).
services/lateness.py, routes/attendance.py."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import AttendanceEvent, PayrollAccrual, PayScheme, PaySchemeLine, Role, User, UserPayScheme
from app.security import hash_secret
from app.services import lateness as ls
from app.services import settings as st
from app.services.attendance import local_tz

from .conftest import login


def _user(db, login_, role_code, hired, day_rate=None):
    u = db.scalar(select(User).where(User.login == login_))
    if u:
        return u
    role = db.scalar(select(Role).where(Role.code == role_code))
    current_branch_id.set(role.branch_id)
    u = User(full_name=f"Тест {login_}", login=login_, role_id=role.id, password_hash=hash_secret(login_ + "123"), hired_on=hired)
    db.add(u)
    db.flush()
    if day_rate:
        s = PayScheme(name=f"Схема {login_}")
        db.add(s)
        db.flush()
        db.add(PaySchemeLine(scheme_id=s.id, kind="salary", salary_period="day", rate=Decimal(day_rate)))
        db.add(UserPayScheme(user_id=u.id, scheme_id=s.id, valid_from=hired))
        db.flush()
    return u


def _ev(db, u, d, hh, mm, direction):
    at = datetime.combine(d, time(hh, mm), tzinfo=local_tz()).astimezone(timezone.utc)
    db.add(AttendanceEvent(user_id=u.id, direction=direction, at=at, method="card"))


def _setup():
    today = date.today()
    W = ls.week_start(today)
    D = W - timedelta(days=7)          # понедельник прошлой недели
    with SessionLocal() as db:
        u = _user(db, "latetest", "mechanic", D, day_rate=1100)        # 1100 ₴ / 11 ч = 100 ₴ в час
        dr = _user(db, "latedir", "director", D)
        bid = u.branch_id
        current_branch_id.set(bid)
        old = st.get(db, bid, "attendance").get("alerts_from")
        st.put(db, bid, "attendance", {"alerts_from": (D - timedelta(days=1)).isoformat()})
        _ev(db, u, D, 8, 20, "in"); _ev(db, u, D, 19, 0, "out")                       # опоздание 20 мин → −1 ч
        _ev(db, u, D + timedelta(1), 8, 10, "in")                                       # 8:10 — норма; не отметил уход
        #                                                                                 D+2 — нет отметок (неявка)
        _ev(db, u, D + timedelta(3), 9, 5, "in"); _ev(db, u, D + timedelta(3), 19, 0, "out")   # 9:05 → неявка, −2 ч
        _ev(db, u, D + timedelta(4), 8, 0, "in"); _ev(db, u, D + timedelta(4), 18, 10, "out")  # ушёл в 18:10 → −1 ч
        _ev(db, u, W, 8, 0, "in"); _ev(db, u, W, 20, 15, "out")                         # задержался на 1 ч 15 мин
        d = W + timedelta(1)
        while d <= today:
            _ev(db, u, d, 7, 55, "in"); _ev(db, u, d, 19, 5, "out")
            d += timedelta(1)
        d = D
        while d <= today:
            _ev(db, dr, d, 8, 30 if d == D else 7, "in"); _ev(db, dr, d, 19, 0, "out")
            d += timedelta(1)
        db.commit()
        return u.id, dr.id, D, W, bid, old


def _boss(login_, about):
    with SessionLocal() as db:
        me = db.scalar(select(User).where(User.login == login_))
        return {(x["f"].day, x["f"].kind): x for x in ls.for_boss(db, me.branch_id, me) if x["f"].user.id == about}


def _accruals(uid):
    with SessionLocal() as db:
        return [(a.amount, a.note) for a in db.scalars(select(PayrollAccrual).where(
            PayrollAccrual.user_id == uid, PayrollAccrual.status != "cancelled"))]


def test_timesheet_rules_money_decisions(client):
    uid, did, D, W, bid, old = _setup()
    try:
        with SessionLocal() as db:
            u = db.get(User, uid)
            got = {(f.day, f.kind): f for f in ls.flags(db, bid, [u])}
        assert got[(D, "late")].hours == 1                                     # 8:20 → целый час
        assert (D + timedelta(1), "late") not in got                           # 8:10 — норма
        assert (D + timedelta(1), "no_out") in got
        assert (D + timedelta(2), "absent") in got
        assert got[(D + timedelta(3), "absent")].hours == 2                    # 9:05 → неявка, 2 часа
        assert got[(D + timedelta(4), "early")].hours == 1                     # 18:10 → 1 час
        assert got[(W, "overtime")].level == "red"                             # задержка больше часа
        assert not any(d.weekday() >= 5 and k in ("late", "absent", "early", "overtime") for d, k in got)

        # руководству (директор): деньги и счёт нарушений
        b = _boss("director", uid)
        assert b[(D, "late")]["amount"] == Decimal("100.00") and b[(D, "late")]["nth"] == 1
        assert b[(D + timedelta(3), "absent")]["amount"] == Decimal("200.00")

        # сотрудник пишет, что делал после работы
        login(client, "latetest", "latetest123")
        page = client.get("/timesheet").text
        assert "Задержались" in page and "Вы задержались после работы" not in page   # 24.11: окошко — под плиткой
        assert "Вы задержались после работы" in client.get("/timesheet?v=over").text
        client.post("/timesheet/note", data={"day": W.isoformat(), "text": "Доделывал машину клиента"})
        client.post("/logout")

        # бухгалтер видит, но не решает; «проверено» не поставить, пока не решено
        login(client, "buh", "buh123")
        assert "Нужно решить" in client.get("/timesheet").text
        page = client.get("/timesheet?v=boss").text
        assert "Ждёт решения директора" in page and 'action="/timesheet/decide"' not in page
        assert client.post("/timesheet/decide", data={"user_id": str(uid), "day": D.isoformat(), "kind": "late",
                                                      "decision": "deduct"}).status_code == 403
        client.post("/logout")

        login(client, "director", "director123")
        dec = lambda day, kind, decision, **kw: client.post("/timesheet/decide", data={
            "user_id": str(uid), "day": day.isoformat(), "kind": kind, "decision": decision, **kw})
        dec(D, "late", "deduct")                                                # 1-е нарушение: −100
        dec(D + timedelta(3), "absent", "excused", reason="car", comment="Сломалась машина")   # не удерживаем
        dec(D + timedelta(4), "early", "deduct")                                # 2-е: −100
        assert _boss("director", uid)[(D + timedelta(2), "absent")]["nth"] == 3  # следующее — уже 3-е
        assert _boss("director", uid)[(D + timedelta(2), "absent")]["fine_due"]
        dec(D + timedelta(2), "absent", "fine", fine="500")                     # 3-е: день не оплачен, штраф 500
        dec(W, "overtime", "bonus", bonus="300", comment="Спасибо")
        acc = _accruals(uid)
        assert (Decimal("-100.00"), f"Опоздание {D:%d.%m}: −1 ч × 100.00 ₴") in acc
        assert sum(a for a, n in acc if n.startswith("Ранний уход")) == Decimal("-100.00")
        assert not any(n.startswith("Неявка") for a, n in acc)                   # уважительная / нет отметок — 0
        assert any(a == Decimal("-500.00") and "3-е нарушение" in n for a, n in acc)
        assert any(a == Decimal("300.00") and n.startswith("Премия: задержался") for a, n in acc)
        # передумали — прежнее удержание отменяется
        dec(D, "late", "leave")
        assert not any(n.startswith("Опоздание") for a, n in _accruals(uid))
        # о себе не решает
        with SessionLocal() as db:
            me_id = db.scalar(select(User.id).where(User.login == "director"))
        r = client.post("/timesheet/decide", data={"user_id": str(me_id), "day": D.isoformat(), "kind": "late",
                                                   "decision": "deduct"})
        assert "О себе решает другой руководитель" in r.text
        # «Отпросился заранее»: сегодня с 15:00 — ранний уход не считается
        client.post("/timesheet/leave", data={"user_id": str(uid), "day": D.isoformat(), "t_from": "08:00",
                                              "t_to": "19:00", "comment": "Звонил директору"})
        with SessionLocal() as db:
            u = db.get(User, uid)
            assert not any(f.day == D and f.kind == "late" for f in ls.flags(db, bid, [u]))
        page = client.get(f"/timesheet?user={uid}&m={D:%Y-%m}").text
        assert "Отпросился 08:00–19:00" in page and "Уважительная причина" in page
        client.post("/logout")

        # бухгалтер больше не упирается в «ждёт решения» по этому человеку за прошлую неделю (кроме «не отметил уход»)
        with SessionLocal() as db:
            assert not ls.undecided(db, bid, uid, D, D + timedelta(6))

        # настройки: бухгалтер не меняет, директор меняет
        login(client, "buh", "buh123")
        assert client.post("/settings/timesheet", data={"fine_amount": "1"}).status_code == 403
        client.post("/logout")
        login(client, "director", "director123")
        client.post("/settings/timesheet", data={"late_min": "15", "absent_min": "60", "over_info_min": "30",
                                                 "over_red_min": "60", "fine_amount": "700", "fine_from": "3"})
        with SessionLocal() as db:
            assert ls.params(db, bid)["fine"] == Decimal(700)
        client.post("/settings/timesheet", data={"late_min": "15", "absent_min": "60", "over_info_min": "30",
                                                 "over_red_min": "60", "fine_amount": "500", "fine_from": "3"})
        client.post("/logout")
    finally:
        with SessionLocal() as db:
            current_branch_id.set(bid)
            st.put(db, bid, "attendance", {"alerts_from": old or date.today().isoformat()})
            for x in (uid, did):
                db.get(User, x).active = False
            db.commit()
