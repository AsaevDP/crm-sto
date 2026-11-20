"""«Напомнить о записи» (20.11.2026, брат): «не приехал» — только через 15 минут после времени записи; машина клиента
уже у нас (открытый наряд) — не «не приехал», а отдельный список. Разные дела одного цвета — разные кружки в меню."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Appointment, Branch, Vehicle, WorkOrder
from app.services import badges
from app.services import schedule as sch


def _appt(db, bid, minutes_ago, name, plate=""):
    a = Appointment(start_at=datetime.now().astimezone() - timedelta(minutes=minutes_ago), duration_min=30,
                    client_name=name, phone="0670000000", plate=plate, status="booked", branch_id=bid)
    db.add(a)
    return a


def test_late_after_15_minutes_and_car_here(client):
    with SessionLocal() as db:
        bid = db.scalar(select(Branch.id))
        current_branch_id.set(bid)
        now_ = datetime.now().astimezone()
        if now_.hour < 1:   # около полуночи «сегодня 20 минут назад» — это вчера; проверка не имеет смысла
            return
        _appt(db, bid, 5, "Рано звонить")
        _appt(db, bid, 20, "Уже опоздал")
        wo = db.scalar(select(WorkOrder).where(WorkOrder.status.not_in(("closed", "cancel"))))
        here_plate = db.get(Vehicle, wo.vehicle_id).plate if wo else ""
        if here_plate:
            _appt(db, bid, 30, "Машина у нас", plate=here_plate)
        db.commit()
        r = sch.reminders(db, bid)
        late = {a.client_name for a in r["late"]}
        assert "Уже опоздал" in late and "Рано звонить" not in late
        if here_plate:
            assert "Машина у нас" not in late and "Машина у нас" in {a.client_name for a in r["here"]}


def test_same_color_different_screens_are_separate():
    out = {}
    badges._add(out, "/orders", "red", 1, "новые — посмотреть", "/orders?tab=intake")
    badges._add(out, "/orders", "red", 1, "вам передали", "/orders?tab=handed")
    badges._add(out, "/orders", "red", 2, "ещё новые", "/orders?tab=intake")
    assert len(out["/orders"]) == 2 and out["/orders"][0]["n"] == 3
    # больше трёх кружков — сливаются сначала менее срочные цвета, красные остаются раздельными
    out = {}
    for c, link in (("red", "/a"), ("red", "/b"), ("orange", "/c"), ("orange", "/d")):
        badges._add(out, "/orders", c, 1, link, link)
    out = badges._compact(out)
    assert [x["c"] for x in out["/orders"]] == ["red", "red", "orange"]
