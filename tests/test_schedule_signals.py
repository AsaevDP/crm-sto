"""«Запись» у мастера — сигналы (21.11.2026, брат): 🟣 новая запись и 🔴 «не приехал» горят, пока мастер не нажал
«✓ Видел»; перенос времени / смена мастера — снова новая; 🟠 сегодня ждём; 🟢 приехал — пока мастер не нажал «Начал»
в наряде. То же у мойщика и детейлера. Цифры у «Записи» в меню."""
import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Appointment, User, WorkOrder, WoWorkSession
from app.models.orders import WO_ACTIVE
from app.services import schedule as svc

from .conftest import login
from .test_step3 import _create


def _user(lg):
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == lg))
        return u.id, u.branch_id


def _appt(name):
    with SessionLocal() as db:
        return db.scalar(select(Appointment).where(Appointment.client_name == name))


def _sig(lg):
    uid, bid = _user(lg)
    with SessionLocal() as db:
        s = svc.master_signals(db, bid, uid)
        return {k: [a.client_name for a in v] for k, v in s.items()}


def test_new_seen_moved_noshow(client):
    andrey, _ = _user("andrey")
    d = (date.today() + timedelta(days=2)).isoformat()
    login(client, "irina", "irina123")
    client.post("/schedule", data={"day": d, "time": "10:00", "duration": "60", "mechanic_id": str(andrey),
                                   "client_name": "Сигнал Новый"})
    client.post("/logout")

    login(client, "andrey", "andrey123")
    assert "Сигнал Новый" in _sig("andrey")["new"]
    page = client.get("/schedule").text
    assert "Новые записи" in page and 'id="zp-sig"' in page and "✓ Видел" in page and "Сигнал Новый" in page
    b = client.get("/api/notify").json()["badges"].get("/schedule", [])
    assert any(x["c"] == "purple" for x in b)
    a = _appt("Сигнал Новый")
    r = client.post(f"/schedule/{a.id}/seen", data={"back": "/schedule"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Сигнал Новый" not in _sig("andrey")["new"]
    client.post("/logout")

    # чужой мастер отметить не может
    login(client, "motor", "motor123")
    assert client.post(f"/schedule/{a.id}/seen", follow_redirects=False).status_code in (403, 303)
    assert _appt("Сигнал Новый").seen_key   # отметка Андрея осталась
    client.post("/logout")

    # перенесли время — снова новая
    login(client, "irina", "irina123")
    client.post(f"/schedule/{a.id}", data={"day": d, "time": "14:00", "duration": "60", "mechanic_id": str(andrey),
                                          "client_name": "Сигнал Новый"})
    assert "Сигнал Новый" in _sig("andrey")["new"]
    # не приехал — красная до «Видел»
    client.post(f"/schedule/{a.id}/status", data={"status": "noshow"})
    s = _sig("andrey")
    assert "Сигнал Новый" in s["noshow"] and "Сигнал Новый" not in s["new"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post("/schedule/all/seen", data={"back": "/schedule"})
    assert "Сигнал Новый" not in _sig("andrey")["noshow"]
    client.post("/logout")


def test_wait_today_and_arrived_until_start(client):
    det, bid = _user("detail")
    t = date.today().isoformat()
    login(client, "irina", "irina123")
    client.post("/schedule", data={"day": t, "time": "23:00", "duration": "30", "mechanic_id": str(det),
                                   "client_name": "Сигнал Мойка"})
    url = _create(client, scenario="A", photos=1, plate="AA7741KC", phone="0671117741").json()["url"]
    client.post("/logout")
    assert "Сигнал Мойка" in _sig("detail")["wait"] and "Сигнал Мойка" in _sig("detail")["new"]
    # приехал: запись связана с открытым нарядом — зелёная, пока детейлер не начал работу в наряде
    current_branch_id.set(bid)
    with SessionLocal() as db:
        wo = db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))
        assert wo.status in WO_ACTIVE
        a = db.scalar(select(Appointment).where(Appointment.client_name == "Сигнал Мойка"))
        db.query(WoWorkSession).filter(WoWorkSession.work_order_id == wo.id, WoWorkSession.mechanic_id == det).delete()
        a.status, a.work_order_id = "arrived", wo.id
        db.commit()
        wid = wo.id
    s = _sig("detail")
    assert "Сигнал Мойка" in s["arrived"] and "Сигнал Мойка" not in s["wait"]
    login(client, "detail", "detail123")
    page = client.get("/schedule").text
    assert "Приехали" in page and f"/orders/{wid}" in page
    assert any(x["c"] == "green" for x in client.get("/api/notify").json()["badges"].get("/schedule", []))
    client.post("/logout")
    with SessionLocal() as db:
        db.add(WoWorkSession(work_order_id=wid, mechanic_id=det, branch_id=bid))
        db.commit()
    assert "Сигнал Мойка" not in _sig("detail")["arrived"]
