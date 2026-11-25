"""25.11.2026 (брат): клиент приехал по записи. «✓ Приехали» нажимает любой менеджер — наряд создаётся сразу
(можно сохранить незаполненным), ведёт и оформляет приёмку менеджер, который записывал: 🔴 кружок, плашка
«Клиент приехал». Мастер наряд не видит, пока не нажали «✓ Принять в работу». Плитки «Записи» — окна со списками."""
import io
import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Appointment, Notification, User, WorkOrder

from .conftest import login


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _jpg():
    from app.demo_data import PHOTOS
    return next(PHOTOS.glob("*.jpg")).read_bytes()


def test_arrived_creates_draft_order(client):
    d = date.today().isoformat()
    _as(client, "irina")   # записывает Ирина — она и ведёт наряд
    client.post("/schedule", data={"day": d, "time": "09:00", "duration": "60", "mechanic_id": str(_uid("andrey")),
                                   "client_name": "Олег Приехавший", "phone": "0673334455", "car": "Mazda CX-5",
                                   "plate": "АА 2525 ВВ", "work": "Стук спереди"})
    with SessionLocal() as db:
        a = db.scalar(select(Appointment).where(Appointment.client_name == "Олег Приехавший"))
    # Окно «Ждём» — клиент с кнопкой «Приехали»
    page = client.get(f"/schedule?d={d}&v=wait").text
    assert "Олег Приехавший" in page and "✓ Приехали" in page and 'id="zp-win"' in page
    # «Приехали» нажимает директор (не тот, кто записывал)
    _as(client, "director")
    r = client.post(f"/schedule/{a.id}/status", data={"status": "arrived", "back": f"/schedule?d={d}&v=wait"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/schedule")
    with SessionLocal() as db:
        a2 = db.get(Appointment, a.id)
        wo = db.get(WorkOrder, a2.work_order_id)
        assert a2.status == "arrived" and wo.intake_pending and wo.status == "created"
        assert wo.manager_id == _uid("irina") and wo.arrived_by == _uid("director") and wo.key_slot
        assert wo.complaint == "Стук спереди" and wo.mechanic_id == _uid("andrey")
        assert db.scalar(select(Notification).where(Notification.user_id == _uid("irina"), Notification.kind == "arrived"))
        wid = wo.id
    # Ирина: красный кружок, плашка «Клиент приехал», окно «Приехали» с «Оформить приёмку»
    _as(client, "irina")
    b = client.get("/api/notify").json()["badges"]
    assert any("клиент приехал по записи" in x["t"] for x in b.get("/orders", []))
    assert "Клиент приехал — оформить приёмку" in client.get("/orders?tab=all").text
    assert "Оформить приёмку" in client.get(f"/schedule?d={d}&v=arrived").text
    assert "Клиент приехал по записи — оформите приёмку" in client.get(f"/orders/{wid}").text
    # Мастер пока не видит наряд
    _as(client, "andrey")
    assert client.get(f"/orders/{wid}").status_code == 403
    assert "Олег Приехавший" not in client.get("/orders?tab=all").text
    # Черновик — без пробега и фото
    _as(client, "irina")
    r = client.post(f"/orders/{wid}/intake", data={"mode": "save", "phone": "0673334455", "name": "Олег Приехавший",
                                                   "plate": "АА 2525 ВВ", "make": "Mazda", "model": "CX-5",
                                                   "complaint": "Стук спереди справа", "scenario": "B", "fuel_level": "50",
                                                   "mechanic_id": str(_uid("andrey"))})
    assert r.json()["ok"]
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        assert wo.intake_pending and wo.complaint == "Стук спереди справа" and wo.fuel_level == 50
    # «Принять в работу» без пробега и фото — нельзя
    base = {"phone": "0673334455", "name": "Олег Приехавший", "plate": "АА 2525 ВВ", "make": "Mazda", "model": "CX-5",
            "complaint": "Стук спереди справа", "scenario": "B", "mechanic_id": str(_uid("andrey"))}
    r = client.post(f"/orders/{wid}/intake", data={**base, "mode": "go"})
    assert r.status_code == 400 and "Пробег" in r.json()["errors"]
    # Другой менеджер (не ведущий) оформлять не может
    with SessionLocal() as db:
        other = db.scalar(select(User).join(User.role).where(User.login != "irina", User.active.is_(True))
                          .where(User.role.has(code="manager")))
    if other:
        _as(client, other.login)
        assert client.get(f"/orders/{wid}/intake").status_code == 403
        _as(client, "irina")
    r = client.post(f"/orders/{wid}/intake", data={**base, "mode": "go", "mileage": "120000"},
                    files=[("photos", ("a.jpg", io.BytesIO(_jpg()), "image/jpeg"))])
    assert r.json()["ok"], r.text
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        assert not wo.intake_pending and wo.status == "diag" and wo.mileage_in == 120000
    b = client.get("/api/notify").json()["badges"]
    assert not any("клиент приехал по записи" in x["t"] for x in b.get("/orders", []))
    _as(client, "andrey")
    assert client.get(f"/orders/{wid}").status_code == 200


def test_noshow_window_and_no_phone(client):
    d = (date.today() + timedelta(days=2)).isoformat()
    _as(client, "irina")
    client.post("/schedule", data={"day": d, "time": "11:00", "duration": "60", "client_name": "Без Телефона",
                                   "car": "Opel Astra", "work": "ТО"})
    with SessionLocal() as db:
        a = db.scalar(select(Appointment).where(Appointment.client_name == "Без Телефона"))
    # Нет телефона и госномера — обычная «Приёмка»
    r = client.post(f"/schedule/{a.id}/status", data={"status": "arrived"}, follow_redirects=False)
    assert r.headers["location"] == f"/orders/new?appt={a.id}"
    client.post(f"/schedule/{a.id}/status", data={"status": "noshow", "back": f"/schedule?d={d}"})
    page = client.get(f"/schedule?d={d}&v=noshow").text
    assert "Без Телефона" in page and "Вернуть в «Ждём»" in page and "Перенести" in page
    client.post(f"/schedule/{a.id}/status", data={"status": "booked", "back": f"/schedule?d={d}&v=noshow"})
    assert "Без Телефона" in client.get(f"/schedule?d={d}&v=wait").text
    assert "Без Телефона" in client.get(f"/schedule?d={d}&v=all").text
    with SessionLocal() as db:
        assert db.get(Appointment, a.id).work_order_id is None
        assert uuid.UUID(str(a.id))
