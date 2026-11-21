"""Наряды в календаре «Записи» (22.11.2026, брат): «Время на выполнение» у работы (не нормо-часы); несделанные работы
открытых нарядов — блоки в колонке мастера; «ждём запчасти» — только если привезут в пределах 3 дней, иначе «вне
графика»; согласование — пунктиром «возможно»; загрузка и свободные окна учитывают наряды."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Purchase, User, WorkOrder, WoWork
from app.services import workload

from .conftest import login
from .test_step3 import _create


def _user(lg):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == lg))


def test_work_minutes_rules():
    assert workload.work_minutes(WoWork(minutes=150, pricing="hours", hours=Decimal("1"), qty=Decimal("1"))) == (150, True)
    assert workload.work_minutes(WoWork(minutes=None, pricing="hours", hours=Decimal("1.5"), qty=Decimal("2"))) == (180, False)
    assert workload.work_minutes(WoWork(minutes=None, pricing="fixed", hours=None, qty=Decimal("1"))) == (60, False)


def test_time_field_and_orders_in_calendar(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 2211 ВВ", phone="0502211000").json()["url"]
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    andrey = _user("andrey")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        current_branch_id.set(wo.branch_id)
        wo.status, wo.mechanic_id = "work", andrey.id
        w = WoWork(work_order_id=wid, name="Замена сцепления", pricing="hours", hours=Decimal("3"), rate=800, qty=1,
                   price=2400, mechanic_id=andrey.id, branch_id=wo.branch_id)
        db.add(w)
        db.commit()
        w_id = w.id
    # менеджер вписывает реальное время — 4,5 ч (нормо-часы 3 — только цена)
    client.post(f"{url}/works/{w_id}", data={"name": "Замена сцепления", "pricing": "hours", "hours": "3", "rate": "800",
                                             "qty": "1", "mechanic_id": str(andrey.id), "time_h": "4,5"})
    with SessionLocal() as db:
        assert db.get(WoWork, w_id).minutes == 270 and db.get(WoWork, w_id).hours == Decimal("3")
    assert 'data-l="Время" class="" title="Время на выполнение">4,5 ч' in client.get(url).text   # столбик «Время»
    with SessionLocal() as db:
        from app.services.orders import staff_by_role
        p = workload.plan(db, db.get(WorkOrder, wid).branch_id, staff_by_role(db, db.get(WorkOrder, wid).branch_id, "masters"))
        b = next(b for b in p["blocks"][andrey.id] if b.wo.id == wid)
        assert b.kind == "solid" and b.minutes >= 270   # 4,5 ч сцепления + другие работы наряда
        first_day = b.segs[0][0].date()
    page = client.get(f"/schedule?d={first_day}&by=mech").text
    assert "zp-wo" in page and "🔧" in page
    assert client.get("/schedule?p=week&by=mech").status_code == 200
    # ждём запчасти, обещают через 10 дней — вне графика; через день — в графике
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        wo.status = "waiting"
        db.add(Purchase(work_order_id=wid, status="ordered", expected_on=date.today() + timedelta(days=10), branch_id=wo.branch_id))
        db.commit()
        bid = wo.branch_id
        from app.services.orders import staff_by_role
        p = workload.plan(db, bid, staff_by_role(db, bid, "masters"))
        assert any(b.wo.id == wid for b in p["off"]) and not any(b.wo.id == wid for b in p["blocks"][andrey.id])
        pu = db.scalar(select(Purchase).where(Purchase.work_order_id == wid))
        pu.expected_on = date.today() + timedelta(days=1)
        db.commit()
        p = workload.plan(db, bid, staff_by_role(db, bid, "masters"))
        b = next(b for b in p["blocks"][andrey.id] if b.wo.id == wid)
        assert b.segs[0][0].date() >= date.today() + timedelta(days=1)
        # согласование — «возможно»
        db.get(WorkOrder, wid).status = "approve"
        db.commit()
        p = workload.plan(db, bid, staff_by_role(db, bid, "masters"))
        assert next(b for b in p["blocks"][andrey.id] if b.wo.id == wid).kind == "tentative"
        db.get(WorkOrder, wid).status = "closed"
        db.commit()
    assert "Наряды в работе" in client.get("/load?p=week").text
