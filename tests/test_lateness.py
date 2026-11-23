"""Табель: опоздания и пропущенные отметки (24.11.2026, брат) — services/lateness.py.
Опоздание ≥ 30 мин — сотруднику 🔴 (крупный) → «Принял к сведению» → 🟠 счёт за неделю. Не пришёл к 11:00 и не
отметил уход — руководству 🔴 «Принял к сведению», сотруднику ⚠ до ручной отметки. Директор / зам опоздал — админу."""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import AttendanceEvent, Role, User
from app.security import hash_secret
from app.services import lateness as ls
from app.services import settings as st
from app.services.attendance import local_tz

from .conftest import login


def _user(db, login_, role_code, hired):
    u = db.scalar(select(User).where(User.login == login_))
    if u:
        return u
    role = db.scalar(select(Role).where(Role.code == role_code))
    current_branch_id.set(role.branch_id)
    u = User(full_name=f"Тест {login_}", login=login_, role_id=role.id, password_hash=hash_secret(login_ + "123"),
             hired_on=hired)
    db.add(u)
    db.flush()
    return u


def _ev(db, u, d, hh, mm, direction):
    at = datetime.combine(d, time(hh, mm), tzinfo=local_tz()).astimezone(timezone.utc)
    db.add(AttendanceEvent(user_id=u.id, direction=direction, at=at, method="card"))


def _setup():
    today = date.today()
    W = ls.week_start(today)          # понедельник этой недели
    D = W - timedelta(days=7)         # понедельник прошлой недели
    with SessionLocal() as db:
        u = _user(db, "latetest", "mechanic", D)
        dr = _user(db, "latedir", "director", D)
        bid = u.branch_id
        current_branch_id.set(bid)
        old = st.get(db, bid, "attendance").get("alerts_from")
        st.put(db, bid, "attendance", {"alerts_from": (D - timedelta(days=1)).isoformat()})
        _ev(db, u, D, 8, 45, "in"); _ev(db, u, D, 18, 0, "out")               # опоздал на 45 мин
        _ev(db, u, D + timedelta(1), 8, 10, "in")                              # не отметил уход
        #                                                                         D+2 — нет отметок (не пришёл)
        _ev(db, u, D + timedelta(3), 11, 30, "in"); _ev(db, u, D + timedelta(3), 18, 0, "out")   # после 11:00 — неявка
        _ev(db, u, D + timedelta(4), 9, 0, "in"); _ev(db, u, D + timedelta(4), 18, 0, "out")     # опоздал на 1 ч
        _ev(db, u, W, 8, 50, "in"); _ev(db, u, W, 8, 55, "out")                # опоздал на этой неделе
        d = W + timedelta(1)
        while d <= today:                                                       # остальные дни недели — вовремя
            _ev(db, u, d, 8, 0, "in"); _ev(db, u, d, 8, 5, "out")
            d += timedelta(1)
        # директор: опоздание (видит админ) и все дни вовремя
        _ev(db, dr, D, 9, 0, "in"); _ev(db, dr, D, 18, 0, "out")
        d = D + timedelta(1)
        while d <= today:
            _ev(db, dr, d, 7, 55, "in"); _ev(db, dr, d, 8, 30, "out")
            d += timedelta(1)
        db.commit()
        return u.id, dr.id, D, W, bid, old


def _mine(uid):
    with SessionLocal() as db:
        u = db.get(User, uid)
        return ls.for_user(db, u.branch_id, u)


def _boss(login_, about):
    with SessionLocal() as db:
        me = db.scalar(select(User).where(User.login == login_))
        return [(f.day, f.kind) for f in ls.for_boss(db, me.branch_id, me) if f.user.id == about]


def test_lateness_flow(client):
    uid, did, D, W, bid, old = _setup()
    try:
        # расчёт
        with SessionLocal() as db:
            u = db.get(User, uid)
            got = {(f.day, f.kind) for f in ls.flags(db, bid, [u])}
        assert (D, "late") in got and (D + timedelta(4), "late") in got and (W, "late") in got
        assert (D + timedelta(1), "no_out") in got
        assert (D + timedelta(2), "absent") in got and (D + timedelta(3), "absent") in got
        assert not any(k == "late" for d, k in got if d == D + timedelta(3))       # после 11:00 — не опоздание
        assert not any(d.weekday() >= 5 and k in ("late", "absent") for d, k in got)   # выходные не считаем

        m = _mine(uid)
        assert len(m["new"]) == 3 and len(m["week"]) == 0 and len(m["warn"]) == 3

        # сотрудник: крупный 🔴, ⚠; «Принял к сведению» → 🟠 за неделю
        login(client, "latetest", "latetest123")
        b = client.get("/api/notify").json()["badges"].get("/timesheet", [])
        red = next(x for x in b if x["c"] == "red")
        assert red["n"] == 3 and red["big"]
        assert any(x.get("shape") == "warn" and x["n"] == 3 for x in b)
        page = client.get("/timesheet").text
        assert "Вы опоздали" in page and "опаздывать больше не буду" in page and "Вы не отметили уход" in page
        client.post("/timesheet/ack", data={"user_id": str(uid), "day": W.isoformat(), "kind": "late", "side": "self"})
        m = _mine(uid)
        assert len(m["new"]) == 2 and len(m["week"]) == 1
        b = client.get("/api/notify").json()["badges"].get("/timesheet", [])
        assert any(x["c"] == "orange" and x["n"] == 1 and "неделе" in x["t"] for x in b)
        # о прогуле / уходе сам себе — нельзя; чужое опоздание — нельзя
        assert client.post("/timesheet/ack", data={"user_id": str(uid), "day": D.isoformat(), "kind": "absent",
                                                   "side": "self"}).status_code == 403
        assert client.post("/timesheet/ack", data={"user_id": str(did), "day": D.isoformat(), "kind": "late",
                                                   "side": "self"}).status_code == 403
        client.post("/logout")

        # руководство: бухгалтер видит неявки и уход, не видит опоздания (ни слесаря, ни директора)
        assert set(_boss("buh", uid)) == {(D + timedelta(1), "no_out"), (D + timedelta(2), "absent"), (D + timedelta(3), "absent")}
        assert _boss("buh", did) == []
        assert (D, "late") in _boss("admin", did)            # опоздание директора — админу
        assert (D, "late") not in _boss("admin", uid)         # опоздание слесаря — только ему самому

        login(client, "buh", "buh123")
        assert any(x["c"] == "red" for x in client.get("/api/notify").json()["badges"].get("/timesheet", []))
        page = client.get("/timesheet").text
        assert "Принять к сведению" in page and "Тест latetest" in page
        # «Принял к сведению» — гаснет у всех руководителей (один за всех)
        client.post("/timesheet/ack", data={"user_id": str(uid), "day": (D + timedelta(2)).isoformat(),
                                            "kind": "absent", "side": "boss"})
        client.post("/timesheet/ack", data={"user_id": str(uid), "day": (D + timedelta(1)).isoformat(),
                                            "kind": "no_out", "side": "boss"})
        assert set(_boss("director", uid)) == {(D + timedelta(3), "absent")}
        # у сотрудника ⚠ по неявке погас (руководство приняло), по уходу — горит, пока не вписали время
        warn = {(f.day, f.kind) for f in _mine(uid)["warn"]}
        assert (D + timedelta(2), "absent") not in warn and (D + timedelta(1), "no_out") in warn
        # бухгалтер вписывает время ухода (раньше — только админ)
        r = client.post("/timesheet/events", data={"user_id": str(uid), "day": (D + timedelta(1)).isoformat(),
                                                   "at": "18:00", "direction": "out", "comment": "сказал, что ушёл в 18"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert (D + timedelta(1), "no_out") not in {(f.day, f.kind) for f in _mine(uid)["warn"]}
        # бухгалтер опоздание слесаря «принять» не может
        assert client.post("/timesheet/ack", data={"user_id": str(uid), "day": D.isoformat(), "kind": "late",
                                                   "side": "boss"}).status_code == 403
        client.post("/logout")

        # директор сам о своём не решает; админ принимает опоздание директора
        login(client, "director", "director123")
        assert client.post("/timesheet/ack", data={"user_id": str(did), "day": D.isoformat(), "kind": "late",
                                                   "side": "boss"}).status_code == 403
        client.post("/logout")
        login(client, "admin", "admin123")
        client.post("/timesheet/ack", data={"user_id": str(did), "day": D.isoformat(), "kind": "late", "side": "boss"})
        assert (D, "late") not in _boss("admin", did)
        client.post("/logout")
    finally:
        with SessionLocal() as db:
            current_branch_id.set(bid)
            st.put(db, bid, "attendance", {"alerts_from": old or date.today().isoformat()})
            for x in (uid, did):
                db.get(User, x).active = False
            db.commit()
