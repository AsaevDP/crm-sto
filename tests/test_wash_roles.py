"""Мойщик и детейлер — разные рабочие места (20.11.2026, брат). Услуга без исполнителя достаётся тому, чей это вид:
мойка — мойщику, полировка (детейлинг) — детейлеру. Цифра «в очереди — никто не взял» у каждого своя;
«пришли запчасти» мойщику и детейлеру не горит."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import WoWork
from app.services import wash as wsvc

from .conftest import login
from .test_wash import _intake, _wo_by_plate
from .test_wash_queue import _user


def _red(client):
    return sum(b["n"] for b in client.get("/api/notify").json()["badges"].get("/orders", []) if b["c"] == "red")


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
    # мойщику эта машина не «никто не взял»
    login(client, "moyka", "moyka123")
    red_washer = _red(client)
    client.post("/wash/take", follow_redirects=False)
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id))
        assert w.mechanic_id != moyka.id   # мойщик полировку не взял
    login(client, "detail", "detail123")
    assert _red(client) >= 1
    client.post("/wash/take", follow_redirects=False)
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id))
        assert w.mechanic_id == detail.id and w.status == "doing"
    login(client, "moyka", "moyka123")
    assert _red(client) <= red_washer


def test_washer_gets_no_parts_badges(client):
    login(client, "moyka", "moyka123")
    b = client.get("/api/notify").json()["badges"].get("/orders", [])
    assert not any("запчасти" in x["t"] for x in b)
