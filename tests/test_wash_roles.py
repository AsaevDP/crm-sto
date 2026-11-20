"""Мойщик и детейлер — разные рабочие места (20.11.2026, брат). Услуга без исполнителя достаётся тому, чей это вид:
мойка — мойщику, полировка (детейлинг) — детейлеру. Цифра 🟠 «ждут мойки — ваши» у каждого своя: ничьи машины его вида
и записанные на него (с ремонта), одна машина — один раз; красной «никто не взял» больше нет.
«Пришли запчасти» мойщику и детейлеру не горит."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import WoWork
from app.services import wash as wsvc

from .conftest import login
from .test_wash import _intake, _wo_by_plate
from .test_wash_queue import _repair_car, _user


def _badges(client):
    return client.get("/api/notify").json()["badges"].get("/orders", [])


def _waiting(client):
    return sum(b["n"] for b in _badges(client) if "ждут мойки" in b["t"])


def test_washer_and_detailer_have_separate_queues(client):
    login(client, "irina", "irina123")
    client.get("/wash/prices")
    assert _intake(client, "АА 7701 РР", "0677770101", ["Полировка кузова"]).json()["ok"]
    client.post("/logout")
    wo = _wo_by_plate("АА 7701 РР")
    current_branch_id.set(wo.branch_id)
    moyka, detail = _user("moyka"), _user("detail")
    with SessionLocal() as db:
        c = next(c for c in wsvc.board(db, wo.branch_id)["queue"] if c.o.id == wo.id)
        assert wsvc.free_works(c, detail) and not wsvc.free_works(c, moyka)   # полировка — детейлеру
        assert not wsvc.can_take(moyka, c) and wsvc.can_take(detail, c)
        ids = lambda u: {x.o.id for x in wsvc.waiting_for(db, wo.branch_id, u)}
        assert wo.id in ids(detail) and wo.id not in ids(moyka)
    login(client, "moyka", "moyka123")
    assert not any(b["c"] == "red" and "никто не взял" in b["t"] for b in _badges(client))
    client.post("/wash/take", follow_redirects=False)
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id))
        assert w.mechanic_id != moyka.id   # мойщик полировку не взял
    login(client, "detail", "detail123")
    before = _waiting(client)
    assert before >= 1
    client.post("/wash/take", follow_redirects=False)
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id))
        assert w.mechanic_id == detail.id and w.status == "doing"
    assert _waiting(client) == before - 1   # взял — теперь это «я мою сейчас», не «ждут»


def test_repair_car_assigned_to_washer_counts(client):
    wo = _repair_car(client, "АА 7702 РР", "0677770102", status="work")   # мойка записана на мойщика
    current_branch_id.set(wo.branch_id)
    with SessionLocal() as db:
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.name == "Замена ремня ГРМ")):
            w.status = "done"   # слесарь закончил — мыть можно
        db.commit()
        assert any(c.o.id == wo.id for c in wsvc.waiting_for(db, wo.branch_id, _user("moyka")))


def test_washer_gets_no_parts_badges(client):
    login(client, "moyka", "moyka123")
    b = client.get("/api/notify").json()["badges"].get("/orders", [])
    assert not any("запчасти" in x["t"] for x in b)
