"""23.11.2026 (брат): шаг «Проблемы» и повторные согласования.
- мастер «Диагностика готова, проблемы впишет менеджер» → наряд ждёт менеджера (🔴, окошко), менеджер вписывает и
  «Проблемы записаны → согласование»;
- менеджеру 🔴 «записать проблемы или отправить на диагностику» сразу после приёмки;
- новая проблема после согласования (ждём запчасти) → снова «Согласование», запчастисту 🔴 «подобрать» и окошко,
  «Согласовал — запчасти не нужны» → наряд туда, где был;
- подписи на плашках: «Менеджер: …», «Подбор · …»."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, User, WorkOrder

from .conftest import agree, login
from .test_step3 import _create


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _badges(client, href="/orders"):
    return client.get("/api/notify").json()["badges"].get(href, [])


def test_diag_by_manager(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="ДП 7301 ДП", phone="0507301000", mechanic_id="").json()["url"]
    assert _wo(url).status == "created"
    assert any(b["c"] == "red" and "отправить на диагностику" in b["t"] for b in _badges(client))
    assert "Менеджер: проблемы или на диагностику" in client.get("/orders?tab=intake").text
    client.post(f"{url}/to-diag", data={"mechanic_id": str(_uid("andrey"))})
    assert _wo(url).status == "diag"
    client.post("/logout")

    login(client, "andrey", "andrey123")
    page = client.get(url).text
    assert "Диагностика готова, проблемы впишет менеджер" in page
    client.post(f"{url}/diag-done", data={"by_manager": "1"})
    wo = _wo(url)
    assert wo.status == "diag" and wo.problems_by_manager
    client.post("/logout")

    login(client, "irina", "irina123")
    with SessionLocal() as db:
        assert db.scalar(select(Notification).where(Notification.work_order_id == wo.id, Notification.kind == "diag_to_mgr"))
    assert any(b["c"] == "red" and "впишите проблемы" in b["t"] for b in _badges(client))
    page = client.get(url).text
    assert "впишите проблемы с его слов" in page and "Менеджер: вписать проблемы" in page
    assert client.post(f"{url}/findings", data={"text": "Люфт рулевой тяги (со слов мастера)", "urgency": "soon"}).json()["ok"]
    client.post(f"{url}/to-approve")
    wo = _wo(url)
    assert wo.status == "approve" and not wo.problems_by_manager


def test_new_problem_while_waiting_restarts_partsman(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="ДП 7302 ДП", phone="0507302000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Колодки (тест круга)", "qty": "1", "cost": "100", "markup_pct": "50",
                                      "action": "order"})
    agree(client, url, then="andrey")
    assert _wo(url).status == "waiting"
    # мастер нашёл новое, пока ждём запчасти — снова «Согласование»
    assert client.post(f"{url}/findings", data={"text": "Течь антифриза", "urgency": "urgent"}).json()["ok"]
    wo = _wo(url)
    assert wo.status == "approve" and wo.picked_at is None
    client.post("/logout")
    # запчастисту — 🔴 «подобрать запчасти» и окошко
    login(client, "oleg", "oleg123")
    assert any(b["c"] == "red" and "подобрать запчасти" in b["t"] for b in _badges(client))
    with SessionLocal() as db:
        assert db.scalar(select(Notification).where(Notification.work_order_id == wo.id, Notification.kind == "new_problem",
                                                    Notification.user_id == _uid("oleg")))
    assert "Подбор к новой проблеме" in client.get("/orders?tab=approve").text
    client.post("/logout")
    # менеджер: 🔴 согласовать; клиент согласился, запчасти не нужны → наряд снова «Ждём запчасти»
    login(client, "irina", "irina123")
    assert any(b["c"] == "red" for b in _badges(client))
    from app.models import WoFinding
    with SessionLocal() as db:
        f = db.scalar(select(WoFinding).where(WoFinding.work_order_id == wo.id))
    client.post(f"{url}/findings/{f.id}/approval", data={"value": "no"})
    client.post(f"{url}/approval-done", data={"no_parts": "1"})
    assert _wo(url).status == "waiting"
