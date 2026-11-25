"""Запись (10.11.2026): цвет мастера, загрузка, свободные окна, «позвонить завтра», неделя с загрузкой."""
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

from .test_step6 import _as


def test_free_windows_and_call_tomorrow(client):
    with SessionLocal() as db:
        sergey = db.scalar(select(User.id).where(User.login == "sergey"))
    _as(client, "irina")
    day = (date.today() + timedelta(days=1)).isoformat()
    client.post("/schedule", data={"day": day, "time": "10:00", "duration": "60", "mechanic_id": str(sergey),
                                   "client_name": "Окна Тестовый", "phone": "0671112299", "car": "Kia Rio"})
    # 25.11.2026: только колонка Сергея — наряды других мастеров из других тестов не мешают
    page = client.get(f"/schedule?d={day}&m={sergey}").text
    # наряды «возможно» (из других тестов; вечером ложатся на завтра) — не записи и не окна: в проверке не участвуют
    import re as _re
    page = _re.sub(r'zp-time">[^<]*</b><span class="zp-st" data-st="tent"', "", page)
    # 22.11.2026 (брат): свободное время — по часу: 08–09, 09–10, (10–11 занято), 11–12 … 18–19
    assert "Свободные окна" in page and "08:00–09:00" in page and "09:00–10:00" in page and "11:00–12:00" in page
    assert "18:00–19:00" in page and "08:00–10:00" not in page
    assert "Записей" in page and "zp-col" in page and "zp-free" in page
    # 20.11.2026 (брат): вместо плитки внизу — кнопка «Напомнить о записи» с оранжевой цифрой, в меню — тоже 🟠
    assert "Напомнить о записи" in client.get("/schedule").text
    rem = client.get("/schedule/remind").text
    assert "Окна Тестовый" in rem and "Позвонил — подтвердил" in rem
    def orange():
        return sum(b["n"] for b in client.get("/api/notify").json()["badges"].get("/schedule", []) if b["c"] == "orange")
    before = orange()
    assert before >= 1
    from app.models import Appointment
    with SessionLocal() as db:
        aid = db.scalar(select(Appointment.id).where(Appointment.client_name == "Окна Тестовый"))
    client.post(f"/schedule/{aid}/called")
    assert orange() == before - 1   # позвонили — из счёта ушла
    assert "Уже позвонили" in client.get("/schedule/remind").text
    week = client.get(f"/schedule?d={day}&view=week&m={sergey}").text   # 22.11.2026: неделя — по одному мастеру
    assert "Окна Тестовый" in week and "zp-bar" in week
