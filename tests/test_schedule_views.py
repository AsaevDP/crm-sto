"""Запись (11.11.2026): день / неделя / месяц / свой период, «по мастерам» и «по датам», загрузка мастеров,
будущие и прошлые записи списком."""
from datetime import date, timedelta

from .conftest import login


def test_schedule_all_views(client):
    login(client, "irina", "irina123")
    d = (date.today() + timedelta(days=2)).isoformat()
    client.post("/schedule", data={"day": d, "time": "10:00", "duration": "120", "client_name": "Клиент вида (тест)",
                                   "phone": "0509990011", "car": "Mazda 6"})
    urls = ["/schedule", "/schedule?p=day&by=date", "/schedule?p=week&by=date", "/schedule?p=week&by=mech",
            f"/schedule?p=month&by=date&d={d}", f"/schedule?p=month&by=mech&d={d}",
            f"/schedule?p=custom&f={date.today().isoformat()}&t={d}&by=date",
            f"/schedule?p=custom&f={date.today().isoformat()}&t={d}&by=mech",
            "/schedule?show=future", "/schedule?show=past&by=mech", "/schedule?view=week"]
    for u in urls:
        r = client.get(u)
        assert r.status_code == 200, u
    assert "Клиент вида (тест)" in client.get("/schedule?show=future").text
    assert "Клиент вида (тест)" in client.get(f"/schedule?p=month&by=date&d={d}").text
    assert "Загрузка мастеров" in client.get("/schedule?p=week&by=mech").text
    client.post("/logout")


def test_period_load_counts_hours():
    from app.db import SessionLocal
    from app.models import Appointment, User
    from app.services import schedule as svc
    from sqlalchemy import select
    with SessionLocal() as db:
        m = db.scalar(select(User).where(User.login == "andrey"))
        a = Appointment(mechanic_id=m.id, start_at=svc.to_utc(date(2030, 1, 7), "09:00"), duration_min=180,
                        status="booked", branch_id=m.branch_id)
        pl = svc.period_load(db, m.branch_id, date(2030, 1, 7), date(2030, 1, 13), [m], [a])
        assert pl[m.id]["hours"] == 3 and pl[m.id]["n"] == 1
        assert pl[m.id]["cap"] == 6 * pl["_day_h"]   # неделя: 6 рабочих дней (вс — выходной)
