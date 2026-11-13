"""Приход и сообщения (11.11.2026): кладовщик принимает на экране «Приход»; когда по наряду пришло ВСЁ —
менеджеру, слесарю, запчастисту одно сообщение «Все запчасти на складе»; слесарь запрашивает выдачу из окошка.
Пометка «срочно» от слесаря видна всем по цепочке; срочная деталь — сообщение сразу."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import IssueRequest, Location, Notification, PurchaseLine, User, WoPart, WorkOrder

from .conftest import agree, login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _notes(login_, url):
    """Сообщения сотрудника по ЭТОМУ наряду (база в тестах общая)."""
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == login_))
        return [(n.kind, n.title) for n in db.scalars(select(Notification).where(
            Notification.user_id == uid, Notification.work_order_id == _wo(url).id))]


def test_receive_all_parts_notifies_team_once(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 8001 КА", phone="0508001000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    for name in ("Фильтр (тест прихода)", "Ремень (тест прихода)"):
        client.post(f"{url}/parts", data={"name": name, "qty": "1", "cost": "100", "markup_pct": "30", "action": "order"})
    agree(client, url)   # 13.11.2026: подбор завершён → менеджер согласовал → заказ
    assert _wo(url).status == "waiting"
    with SessionLocal() as db:
        parts = db.scalars(select(WoPart).where(WoPart.work_order_id == _wo(url).id)).all()
        belt = next(p for p in parts if p.name.startswith("Ремень"))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == belt.id))
    client.post(f"/purchases/{pid}", data={"action": "ordered"})
    client.post("/logout")

    # Слесарь: ремень нужен срочно → запчастисту и менеджеру сообщение, всем видна пометка
    login(client, "andrey", "andrey123")
    assert "🔥 Срочно" in client.get(url).text
    client.post(f"{url}/parts/{belt.id}/urgent")
    client.post("/logout")
    assert any(k == "urgent_ask" for k, _ in _notes("oleg", url)) and any(k == "urgent_ask" for k, _ in _notes("irina", url))
    assert not _notes("andrey", url)                                    # себе не пишем

    # Кладовщик: экран «Приход», срочное сверху; принимает ремень → слесарю «Срочная деталь пришла», наряд ещё ждёт
    login(client, "pavel", "pavel123")
    client.post("/stock/locations", data={"rack": "R", "shelves": "1", "places": "1"})
    page = client.get("/stock/receive").text
    assert "Приход" in page and "🔥 срочно" in page and page.index("Ремень (тест прихода)") < page.index("Фильтр (тест прихода)")
    assert 'href="/stock/receive"' in client.get("/stock").text   # плитка «Приход» на складе
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "R"))
        belt_line = db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == belt.id))
    client.post("/stock/receive", data={"line_id": str(belt_line.id), f"loc_{belt_line.id}": str(loc.id)})
    assert _wo(url).status == "waiting"
    assert [k for k, _ in _notes("andrey", url)] == ["urgent_in"]
    assert any(k == "urgent_in" for k, _ in _notes("oleg", url))         # 12.11.2026: и запчастисту, и менеджеру
    assert not any(k == "parts_ready" for k, _ in _notes("irina", url))

    # Принять всё остальное → «Все запчасти на складе» менеджеру, слесарю, запчастисту — по одному разу
    r = client.post("/stock/receive", data={"all": str(pid)})   # ответ — страница «Приход» после приёма
    assert _wo(url).status == "instock"
    assert "Напечатать наклейки" in r.text and "сообщение ушло менеджеру и слесарю" in r.text
    client.post("/logout")
    for who in ("irina", "andrey", "oleg"):
        assert [k for k, _ in _notes(who, url)].count("parts_ready") == 1, who

    # Слесарь: окошко с кнопкой «Запросить выдачу» → запрос у кладовщика
    login(client, "andrey", "andrey123")
    notes = client.get("/api/notify", headers=FETCH).json()["notes"]
    ready = next(n for n in notes if n["kind"] == "parts_ready")
    assert ready["request_url"].endswith("/issue-request")
    client.post(ready["request_url"], headers=FETCH)
    client.post(f"/api/notify/{ready['id']}/read", headers=FETCH)
    with SessionLocal() as db:
        assert db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == _wo(url).id, IssueRequest.status == "new"))
    assert all(n["id"] != ready["id"] for n in client.get("/api/notify", headers=FETCH).json()["notes"])
    assert "Все запчасти на складе" in client.get("/notifications").text
    client.post("/logout")

    # Кладовщик выдаёт — запрос закрыт, наряд «В работе»
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id, IssueRequest.status == "new"))
    client.post(f"/stock/issues/{rid}/issue")
    assert _wo(url).status == "work"
    client.post("/logout")


def test_default_cell_is_where_part_already_lies(client):
    from app.services import stock as svc
    # Повторный приход той же запчасти без выбора ячейки — в ту же ячейку
    with SessionLocal() as db:
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.received.is_(True), PurchaseLine.location_id.is_not(None)))
        if line is None:
            return
        assert svc.default_location(db, line.catalog_id) == line.location_id
