"""Свежие сверху (18.11.2026, брат): плитки нарядов — по последнему действию (activity_at). Любое изменение по наряду
(работа, запчасть, оплата, статус…) поднимает его наверх; просмотр — нет. На плитке — «изм. ЧЧ:ММ · кто»."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User, WorkOrder
from app.services import orders as osvc

from .conftest import login


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _pos(html, number):
    return html.find(number)


def test_last_action_moves_order_up(client):
    from .test_step3 import _create
    login(client, "irina", "irina123")
    a = _create(client, scenario="A", photos=1, plate="КА 7101 КА", phone="0507101000").json()["url"]
    b = _create(client, scenario="A", photos=1, plate="КА 7102 КА", phone="0507102000").json()["url"]
    na, nb = _wo(a).number, _wo(b).number
    page = client.get("/orders").text
    assert _pos(page, nb) < _pos(page, na)          # новее — выше
    before = _wo(a).activity_at
    client.get(a)                                   # просмотр — не действие
    assert _wo(a).activity_at == before
    client.post(f"{a}/works", data={"name": "Тест свежести", "pricing": "fixed", "rate": "100"})
    wa = _wo(a)
    assert wa.activity_at > before
    with SessionLocal() as db:
        assert wa.activity_by == db.scalar(select(User.id).where(User.login == "irina"))
    page = client.get("/orders").text
    assert _pos(page, na) < _pos(page, nb)          # действие по старому наряду — он наверху
    assert "изм. " in page
    client.post("/logout")


def test_fill_activity_from_audit(client):
    from .test_step3 import _create
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 7103 КА", phone="0507103000").json()["url"]
    client.post(f"{url}/works", data={"name": "Из журнала", "pricing": "fixed", "rate": "100"})
    client.post("/logout")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, _wo(url).id)
        real = wo.activity_at
        wo.activity_at = None
        db.commit()
        osvc.fill_activity(db)
        db.commit()
        wo = db.get(WorkOrder, wo.id)
        assert wo.activity_at is not None and abs((wo.activity_at - real).total_seconds()) < 5
