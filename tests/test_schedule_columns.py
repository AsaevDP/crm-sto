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


def test_free_time_by_hour_and_week_day_button(client):
    from app.routes.schedule import _hourly
    assert _hourly([("08:00", "11:00", 180)]) == [("08:00", "09:00", 60), ("09:00", "10:00", 60), ("10:00", "11:00", 60)]
    assert _hourly([("15:30", "17:15", 105)]) == [("15:30", "16:00", 30), ("16:00", "17:00", 60)]   # 15 мин в конце — нет
    assert _hourly([("15:45", "16:00", 15)]) == []
    login(client, "irina", "irina123")
    andrey = _uid("andrey")
    d = date.today() + timedelta(days=7)
    while d.weekday() == 6:
        d += timedelta(days=1)
    week = client.get(f"/schedule?p=week&d={d}&m={andrey}").text
    # день недели — кнопка «записать на этот день» с мастером и первым свободным часом, без перехода на «день»
    assert f'data-day="{d}"' in week and "zp-col-new" in week and f'data-mech="{andrey}"' in week
    assert "08:00–09:00" in week
    client.post("/logout")


def test_week_month_one_master_and_weekend(client):
    """22.11.2026 (брат): выходные по умолчанию — сб и вс (записать «всё равно» можно); неделя и месяц — только по
    одному мастеру (без «Все мастера»), не выбран — первый; запись без мастера видна с подписью."""
    from app.services import settings as st
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == "irina"))
        assert set(st.get(db, u.branch_id, "schedule")["days_off"]) == {5, 6}
    login(client, "irina", "irina123")
    d = date.today() + timedelta(days=14)
    sat = d + timedelta(days=(5 - d.weekday()) % 7)
    client.post("/schedule", data={"day": sat.isoformat(), "time": "10:00", "duration": "60", "client_name": "Суббота Тест"})
    for p_ in ("week", "month"):
        page = client.get(f"/schedule?p={p_}&d={sat}").text
        assert "Все мастера" not in page and "zp-mchip on" in page, p_
        assert "Суббота Тест" in page and "Без мастера" in page or p_ == "month", p_
        assert "всё равно" in page, p_
    month = client.get(f"/schedule?p=month&d={sat}").text
    assert "Суббота Тест" in month and "свободно" in month and "zp-day-new" in month
    assert "Все мастера" in client.get(f"/schedule?p=day&d={sat}").text   # в «Дне» — как было
    client.post("/logout")
