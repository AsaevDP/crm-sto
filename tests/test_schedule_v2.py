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
    page = client.get(f"/schedule?d={day}").text
    assert "Свободные окна" in page and "08:00–10:00" in page and "11:00–19:00" in page
    assert "Записей: " in page and "sg-load" in page
    assert "Позвонить: завтра" in client.get("/schedule").text      # сегодня видно, кому звонить на завтра
    week = client.get(f"/schedule?d={day}&view=week").text
    assert "Окна Тестовый" in week and "loadbar" in week
