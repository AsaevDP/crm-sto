"""«Запись» столбиками (22.11.2026, брат): день — столбик на мастера, неделя — на день, месяц — календарь; плитки,
кнопки «Новая запись», «Свободные окна», «Напомнить о записи», «Загрузка мастеров»; выбор мастера; у мастера — только
его столбик без кнопок записи; «Все записи списком»; после записи — назад на тот же экран."""
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Appointment, User

from .conftest import login


def _uid(lg):
    with SessionLocal() as db:
        return str(db.scalar(select(User.id).where(User.login == lg)))


def test_manager_columns_and_buttons(client):
    login(client, "irina", "irina123")
    andrey = _uid("andrey")
    d = (date.today() + timedelta(days=2)).isoformat()
    back = f"/schedule?p=week&d={d}&m={andrey}"
    r = client.post("/schedule", data={"day": d, "time": "11:00", "duration": "60", "mechanic_id": andrey,
                                       "client_name": "Столбик Тестовый", "phone": "0671239876", "back": back},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back   # вернулись на ту же неделю и мастера
    day = client.get(f"/schedule?p=day&d={d}").text
    for s in ("zp-col", "Столбик Тестовый", "Новая запись", "Свободные окна", "Напомнить о записи",
              "Загрузка мастеров", "Все мастера", "Все записи списком", "Ждём", "Приехали", "Не приехали"):
        assert s in day, s
    assert "data-new" in day and 'id="zp-free"' in day and 'id="appt-form"' in day
    week = client.get(back).text
    assert "Столбик Тестовый" in week and "zp-cols week" in week and "zp-mchip on" in week
    assert "/load?p=week" in week and "from=schedule" in week
    month = client.get(f"/schedule?p=month&d={d}").text
    assert "zp-cal" in month and "Столбик Тестовый" in month
    with SessionLocal() as db:
        aid = db.scalar(select(Appointment.id).where(Appointment.client_name == "Столбик Тестовый"))
    edit = client.get(f"/schedule?p=day&d={d}&edit={aid}").text
    assert 'id="zp-edit"' in edit and "Приехал → приёмка" in edit
    assert "Столбик Тестовый" in client.get("/schedule?show=future").text
    assert 'href="/schedule"' in client.get("/load?p=week&from=schedule").text   # «назад» — в «Запись»
    client.post("/logout")


def test_master_sees_only_own_column(client):
    login(client, "irina", "irina123")
    andrey, other = _uid("andrey"), _uid("motor")
    d = (date.today() + timedelta(days=3)).isoformat()
    client.post("/schedule", data={"day": d, "time": "10:00", "duration": "60", "mechanic_id": andrey, "client_name": "Свой Клиент"})
    client.post("/schedule", data={"day": d, "time": "12:00", "duration": "60", "mechanic_id": other, "client_name": "Чужой Клиент"})
    client.post("/logout")
    login(client, "andrey", "andrey123")
    for v in (f"?p=day&d={d}", f"?p=week&d={d}", f"?p=month&d={d}"):
        page = client.get("/schedule" + v).text
        assert "Свой Клиент" in page and "Чужой Клиент" not in page
        assert "Новая запись" not in page and "Все мастера" not in page and "Свободные окна" not in page
    with SessionLocal() as db:
        aid = db.scalar(select(Appointment.id).where(Appointment.client_name == "Чужой Клиент"))
    assert '<dialog id="zp-edit"' not in client.get(f"/schedule?p=day&d={d}&edit={aid}").text   # чужую не открыть
    client.post("/logout")
