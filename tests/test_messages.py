"""Сообщения по наряду (11.11.2026): слесарь смотрит склад без цен, просит запчасть под наряд → менеджер согласует →
кладовщик выдаёт только после согласования. Текстовые сообщения команде наряда."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import (IssueRequest, IssueRequestLine, Notification, OrderMessage, StockItem, User, WoPart)
from app.services import stock as ssvc

from .test_step6 import _as, _order_to_work, _wo


def _uid(login):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login))


def _stock(name, qty=5, cost="100"):
    """Позиция на складе с остатком (как после прихода)."""
    with SessionLocal() as db:
        current_branch_id.set(db.get(User, _uid("pavel")).branch_id)
        cat = ssvc.get_or_create_catalog(db, name, "", "Bosch")
        item = ssvc.get_or_create_item(db, cat.id, None)
        item.cost = Decimal(cost)
        ssvc.move(db, "in", item, qty, comment="тест")
        db.commit()
        return item.id


def _msgs(url):
    with SessionLocal() as db:
        return db.scalars(select(OrderMessage).where(OrderMessage.work_order_id == _wo(url).id)
                          .order_by(OrderMessage.created_at)).all()


def test_mechanic_asks_manager_approves_storekeeper_issues(client):
    url = _order_to_work(client, "КМ 2001 КМ", "0502001000", "Колодки (сообщения)")
    item = _stock("Очиститель тормозов (сообщения)", qty=5, cost="100")
    # Слесарь видит склад: название и количество, без цен, ячеек и управления
    _as(client, "andrey")
    page = client.get("/stock?sec=repair").text
    assert "Очиститель тормозов (сообщения)" in page and "Ячейки" not in page and "закупочным" not in page
    card = client.get(f"/stock/items/{item}").text
    assert "Попросить под наряд" in card and "Движения" not in card and "₴" not in card
    # Больше, чем есть, — нельзя
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "9"})
    assert _msgs(url) == []
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "2",
                                       "text": "Суппорт грязный, нужно промыть"})
    m = _msgs(url)[0]
    assert (m.kind, m.status, m.qty) == ("part", "pending", Decimal("2.00"))
    # Склад не тронут, на выдачу ещё не ушло
    with SessionLocal() as db:
        assert ssvc.catalog_free(db, db.get(StockItem, item).branch_id, [m.catalog_id])[m.catalog_id] == 5
        assert db.scalar(select(Notification).where(Notification.user_id == _uid("irina"), Notification.kind == "part_ask"))
        assert db.scalar(select(Notification).where(Notification.user_id == _uid("pavel"), Notification.kind == "part_ask_info"))
    # Кладовщик видит «ждёт согласования», но выдать нечего
    _as(client, "pavel")
    iss = client.get("/stock/issues").text
    assert "ждёт согласования менеджера" in iss and "Очиститель тормозов (сообщения)" in iss
    # Слесарь не может согласовать сам
    _as(client, "andrey")
    assert client.post(f"/messages/{m.id}/decide", data={"action": "approve"}).status_code == 403
    # Менеджер согласует с ценой 150 → в наряде, в резерве, кладовщику на выдачу
    _as(client, "irina")
    assert "Согласовать" in client.get("/notifications").text
    client.post(f"/messages/{m.id}/decide", data={"action": "approve", "price": "150"})
    m = _msgs(url)[0]
    assert m.status == "approved"
    with SessionLocal() as db:
        part = db.get(WoPart, m.wo_part_id)
        assert (part.status, part.price, part.qty) == ("reserved", Decimal("150.00"), Decimal("2.00"))
        line = db.scalar(select(IssueRequestLine).where(IssueRequestLine.wo_part_id == part.id))
        req = db.get(IssueRequest, line.request_id)
        assert req.status == "new" and req.created_by == _uid("andrey")
    # Кладовщик выдаёт
    _as(client, "pavel")
    client.post(f"/stock/issues/{req.id}/issue")
    with SessionLocal() as db:
        assert db.get(WoPart, m.wo_part_id).status == "issued"
        assert ssvc.catalog_free(db, req.branch_id, [m.catalog_id])[m.catalog_id] == 3
    # Повторно согласовать нельзя
    _as(client, "irina")
    client.post(f"/messages/{m.id}/decide", data={"action": "approve"})
    with SessionLocal() as db:
        assert db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id, WoPart.name == "Очиститель тормозов (сообщения)",
                                              WoPart.id != m.wo_part_id)) is None


def test_decline_and_text_messages(client):
    url = _order_to_work(client, "КМ 2002 КМ", "0502002000", "Колодки (отказ)")
    item = _stock("Смазка суппорта (сообщения)", qty=1)
    _as(client, "andrey")
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "1"})
    m = _msgs(url)[0]
    _as(client, "irina")
    client.post(f"/messages/{m.id}/decide", data={"action": "decline", "answer": ""})   # без причины — нельзя
    assert _msgs(url)[0].status == "pending"
    client.post(f"/messages/{m.id}/decide", data={"action": "decline", "answer": "Клиент отказался"})
    assert _msgs(url)[0].status == "declined"
    with SessionLocal() as db:
        assert db.scalar(select(Notification).where(Notification.user_id == _uid("andrey"), Notification.kind == "part_no"))
        assert db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id, WoPart.name == "Смазка суппорта (сообщения)")) is None
    # Текст: слесарь пишет менеджеру и запчастисту — «нет на складе, закажите»
    _as(client, "andrey")
    client.post("/messages", data={"order_id": str(_wo(url).id), "to": ["manager", "partsman"],
                                   "text": "Нужен хомут глушителя 50 мм — на складе нет, закажите"})
    t = [x for x in _msgs(url) if x.kind == "text"][0]
    assert "хомут" in t.text
    with SessionLocal() as db:
        got = {db.get(User, n.user_id).login for n in db.scalars(select(Notification).where(Notification.kind == "msg",
                                                                                           Notification.work_order_id == _wo(url).id))}
    assert got == {"irina", "oleg"}
    _as(client, "irina")
    assert "хомут глушителя" in client.get("/notifications").text
    assert "хомут глушителя" in client.get(url).text


def test_mechanic_cannot_ask_for_foreign_order(client):
    url = _order_to_work(client, "КМ 2003 КМ", "0502003000", "Колодки (чужой)")   # слесарь — andrey
    item = _stock("Герметик (сообщения)", qty=3)
    _as(client, "sergey")
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "1"})
    assert _msgs(url) == []
    assert client.post("/messages", data={"order_id": str(_wo(url).id), "to": ["manager"], "text": "привет"}).status_code == 403
