"""Заказы запчастистов (12.11.2026): у каждого свои закупки, под наряд — отдельная на каждый наряд;
«Мои заказы» у запчастиста; «Заказы запчастистов» у кладовщика и директора; «пришло» — по приходу на склад."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Purchase, PurchaseLine, User, WoPart

from .test_step3 import _create
from .test_step6 import _as, _wo


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _purchase_of(part_name):
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.name == part_name))
        return db.get(Purchase, db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id)))


def test_partsman_orders(client):
    _as(client, "irina")
    url_a = _create(client, scenario="A", photos=1, plate="KA1001AA", phone="0670001001", works="Колодки").json()["url"]
    url_b = _create(client, scenario="A", photos=1, plate="KA1002AA", phone="0670001002", works="Фильтр").json()["url"]

    # Олег заказывает под два наряда (в первый — с запасом: нужно 1, берём 3), админ — тоже в первый
    _as(client, "oleg")
    client.post(f"{url_a}/parts", data={"name": "Колодки ПО-тест", "qty": "1", "cost": "100", "markup_pct": "50",
                                        "action": "order", "order_qty": "3"})
    client.post(f"{url_b}/parts", data={"name": "Фильтр ПО-тест", "qty": "1", "cost": "50", "markup_pct": "50", "action": "order"})
    _as(client, "admin")
    client.post(f"{url_a}/parts", data={"name": "Датчик ПО-тест", "qty": "1", "cost": "80", "markup_pct": "50", "action": "order"})

    pa, pb, pc = _purchase_of("Колодки ПО-тест"), _purchase_of("Фильтр ПО-тест"), _purchase_of("Датчик ПО-тест")
    oleg, admin = _uid("oleg"), _uid("admin")
    assert len({pa.id, pb.id, pc.id}) == 3                     # одна накладная — один наряд, у каждого свои
    assert pa.created_by == oleg and pb.created_by == oleg and pc.created_by == admin
    assert pa.work_order_id == _wo(url_a).id and pb.work_order_id == _wo(url_b).id

    # Закупки: видно, кто заказал и под какой наряд; фильтр по запчастисту
    page = client.get("/purchases?tab=all").text
    assert "Заказал · наряд" in page and _wo(url_a).number in page
    page = client.get(f"/purchases?tab=all&who={oleg}").text
    assert f"/purchases/{pa.id}" in page and f"/purchases/{pc.id}" not in page
    assert "Заказал: <b>Олег Запчастин" in client.get(f"/purchases/{pa.id}").text

    # Меню: «Мои заказы» — только у запчастиста; «Заказы запчастистов» — у кладовщика, директора, админа
    assert 'href="/my-orders"' not in client.get("/purchases").text
    _as(client, "oleg")
    menu = client.get("/purchases").text
    assert 'href="/my-orders"' in menu and 'href="/purchases/by-partsman"' not in menu
    mine = client.get("/my-orders").text
    assert "Колодки ПО-тест" in mine and "Фильтр ПО-тест" in mine and "Датчик ПО-тест" not in mine
    assert "в наряд 1, на склад 2" in mine
    assert client.get("/purchases/by-partsman").status_code == 403

    # Отправили поставщику → кладовщик принял → у Олега «пришло»
    client.post(f"/purchases/{pa.id}", data={"action": "ordered"})
    _as(client, "pavel")
    menu = client.get("/stock").text
    assert 'href="/purchases/by-partsman"' in menu and 'href="/my-orders"' not in menu
    board = client.get(f"/purchases/by-partsman?u={oleg}").text
    assert "Олег Запчастин" in board and "Колодки ПО-тест" in board and "Принять на склад" in board
    assert "заказал Запчастин" in client.get(f"/stock/receive?who={oleg}").text
    client.post(f"/purchases/{pa.id}/receive", data={})
    _as(client, "oleg")
    arrived = client.get("/my-orders?st=arrived").text
    assert "Колодки ПО-тест" in arrived and "Фильтр ПО-тест" not in arrived

    # Директор видит, слесарь — нет
    _as(client, "director")
    assert "Колодки ПО-тест" in client.get(f"/purchases/by-partsman?u={oleg}&st=arrived").text
    assert "Фильтр ПО-тест" in client.get(f"/purchases/by-partsman?u={oleg}").text   # по умолчанию — что ещё не пришло
    _as(client, "andrey")
    assert client.get("/purchases/by-partsman").status_code == 403 and client.get("/my-orders").status_code == 403
