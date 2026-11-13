"""Заказы запчастистов (12.11.2026): у каждого свои закупки, под наряд — отдельная на каждый наряд;
«Мои заказы» у запчастиста; «Заказы запчастистов» у кладовщика и директора; «пришло» — по приходу на склад."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Purchase, PurchaseLine, User, WoPart

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _purchase_of(part_name):
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.name == part_name))
        return db.get(Purchase, db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id)))


def _purchase_line(part_name):
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.name == part_name))
        return db.scalar(select(PurchaseLine.id).where(PurchaseLine.wo_part_id == part.id))


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
    agree(client, url_a, then=None)   # 13.11.2026: заказ — только после «Согласовал с клиентом»
    agree(client, url_b, then="admin")

    pa, pb, pc = _purchase_of("Колодки ПО-тест"), _purchase_of("Фильтр ПО-тест"), _purchase_of("Датчик ПО-тест")
    oleg = _uid("oleg")
    # одна накладная — один наряд; под наряд заказ всегда у запчастиста этого наряда, кто бы ни добавил (13.11.2026)
    assert pa.id == pc.id and pa.id != pb.id
    assert pa.created_by == oleg and pb.created_by == oleg
    assert pa.work_order_id == _wo(url_a).id and pb.work_order_id == _wo(url_b).id

    # Закупки: видно, кто заказал и под какой наряд; фильтр по запчастисту
    page = client.get("/purchases?tab=all").text
    assert "Заказал · наряд" in page and _wo(url_a).number in page
    page = client.get(f"/purchases?tab=all&who={oleg}").text
    assert f"/purchases/{pa.id}" in page and f"/purchases/{pb.id}" in page
    assert "Заказал: <b>Олег Запчастин" in client.get(f"/purchases/{pa.id}").text

    # Меню: «Мои заказы» — только у запчастиста; «Заказы запчастистов» — у кладовщика, директора, админа
    assert 'href="/my-orders"' not in client.get("/purchases").text
    _as(client, "oleg")
    menu = client.get("/purchases").text
    assert 'href="/my-orders"' in menu and 'href="/purchases/by-partsman"' not in menu
    mine = client.get("/my-orders").text
    assert "Колодки ПО-тест" in mine and "Фильтр ПО-тест" in mine and "Датчик ПО-тест" in mine
    assert "Заказать по нарядам" in mine and "✓ Заказал весь наряд" in mine
    assert "в наряд 1, на склад 2" in mine
    assert client.get("/purchases/by-partsman").status_code == 403

    # «✓ Заказал» — прямо в «Моих заказах», у каждого черновика; поставщик и прочее не стираются
    assert f'action="/purchases/{pa.id}/ordered"' in mine and f'action="/purchases/{pb.id}/ordered"' in mine
    client.post(f"/purchases/{pa.id}", data={"action": "save", "note": "до 16:00"})
    r =client.post(f"/purchases/{pa.id}/ordered", data={"back": "/my-orders"}, follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/my-orders"
    with SessionLocal() as db:
        p = db.get(Purchase, pa.id)
        assert p.status == "ordered" and p.ordered_on and p.note == "до 16:00"
    assert f'action="/purchases/{pa.id}/ordered"' not in client.get("/my-orders").text
    _as(client, "irina")   # менеджер — только смотрит
    assert client.post(f"/purchases/{pb.id}/ordered", data={}).status_code == 403
    _as(client, "oleg")

    # Отправили поставщику. «Привезли» и приёмку отмечает только кладовщик — запчастисту нельзя
    client.post(f"/purchases/{pb.id}", data={"action": "ordered"})
    assert client.post(f"/purchases/{pa.id}/delivered", data={}).status_code == 403
    assert client.post(f"/purchases/{pa.id}/receive", data={}).status_code == 403
    assert "🚚 Привезли" not in client.get(f"/purchases/{pa.id}").text
    _as(client, "pavel")
    page = client.get(f"/purchases/{pa.id}").text
    assert "🚚 Привезли" in page and "Принять полностью" not in page
    client.post(f"/purchases/{pa.id}/delivered", data={"invoice": "НК-777"})
    with SessionLocal() as db:
        p_ = db.get(Purchase, pa.id)
        assert p_.status == "delivered" and p_.supplier_invoice == "НК-777" and p_.delivered_by == _uid("pavel")
    page = client.get("/purchases?tab=delivered").text
    assert "Привезли — ждут приёмки" in page and f"/purchases/{pa.id}" in page and "Принять полностью" in page
    _as(client, "oleg")
    assert "привезли, кладовщик принимает" in client.get("/my-orders").text
    _as(client, "pavel")
    # Неполная поставка по второму наряду: привезли 0 из 1 → ничего не принято; в первом — потом полностью
    client.post(f"/purchases/{pb.id}/delivered", data={})
    lb = _purchase_line("Фильтр ПО-тест")
    client.post(f"/purchases/{pb.id}/receive", data={"all": str(pb.id), f"qty_{lb}": "0"})
    with SessionLocal() as db:
        assert db.get(Purchase, pb.id).status == "delivered"
    la = _purchase_line("Колодки ПО-тест")
    client.post(f"/purchases/{pa.id}/receive", data={"all": str(pa.id), f"qty_{la}": "1"})   # из 3 привезли 1
    with SessionLocal() as db:
        assert db.get(Purchase, pa.id).status == "partial"
    assert "Неполная поставка" in client.get(f"/purchases/{pa.id}").text
    client.post(f"/purchases/{pa.id}/delivered", data={})   # довезли остальное
    assert "Принять полностью" in client.get(f"/purchases/by-partsman?u={oleg}").text
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
