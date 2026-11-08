"""Табель: проходная по карте, часы, ручная отметка, оклад из табеля."""
from datetime import date

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AccessPoint, AttendanceEvent, User
from app.services.attendance import compute_days, days_present

from .conftest import login


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def test_kiosk_requires_device_setup(client):
    r = client.post("/kiosk/tap", data={"uid": "0000100006"})
    assert r.status_code == 403
    assert "не проходная" in client.get("/kiosk").text


def test_kiosk_tap_in_out_and_timesheet(client):
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        gate = db.scalar(select(AccessPoint).where(AccessPoint.kind == "gate"))
    r = client.post(f"/settings/points/{gate.id}/kiosk", follow_redirects=False)
    assert r.headers["location"] == "/kiosk" and "crm_kiosk" in r.headers.get("set-cookie", "")
    client.post("/logout")  # проходная работает без входа в CRM
    a = client.post("/kiosk/tap", data={"uid": "0000100006"}).json()
    assert a["ok"] and a["direction"] == "in" and "Андрей" in a["name"]
    again = client.post("/kiosk/tap", data={"uid": "0000100006"}).json()  # двойной пик — игнор
    assert again["direction"] == "in"
    assert client.post("/kiosk/tap", data={"uid": "999"}).status_code == 404
    with SessionLocal() as db:
        evs = db.scalars(select(AttendanceEvent).where(AttendanceEvent.user_id == _uid("andrey"))).all()
    assert len(evs) == 1

    # Главный админ добавляет «ушёл» вручную — только с причиной
    login(client, "admin", "admin123")
    today = date.today().isoformat()
    client.post("/timesheet/events", data={"user_id": str(_uid("andrey")), "day": today, "at": "23:59", "direction": "out", "comment": ""})
    with SessionLocal() as db:
        assert len(db.scalars(select(AttendanceEvent).where(AttendanceEvent.user_id == _uid("andrey"))).all()) == 1
    client.post("/timesheet/events", data={"user_id": str(_uid("andrey")), "day": today, "at": "23:59", "direction": "out",
                                           "comment": "Ушёл без отметки"})
    with SessionLocal() as db:
        evs = db.scalars(select(AttendanceEvent).where(AttendanceEvent.user_id == _uid("andrey")).order_by(AttendanceEvent.at)).all()
        days = compute_days(evs)
        assert days[date.today()].minutes > 0 and not days[date.today()].open
        assert days_present(db, evs[0].branch_id, _uid("andrey"), date.today().year, date.today().month) == 1
    assert "Андрей Демченко" in client.get("/timesheet").text
    client.post("/logout")

    # Слесарь видит только свой табель, править не может
    login(client, "andrey", "andrey123")
    page = client.get("/timesheet").text
    assert "Мой табель" in page and "Ручная отметка" not in page
    assert client.post("/timesheet/events", data={"user_id": str(_uid("andrey")), "day": today, "at": "10:00",
                                                  "direction": "in", "comment": "x"}).status_code == 403
