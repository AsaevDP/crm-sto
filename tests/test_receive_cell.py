"""Приёмка без ячейки (19.11.2026, брат, наряд 61): номер накладной вписали, ячейку не выбрали — не принимаем."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Location, PurchaseLine

from .conftest import login


def test_receive_requires_cell(client):
    login(client, "pavel", "pavel123")
    r = client.post("/purchases/new", data={"supplier_new": "Ячейка-тест"})
    pid = uuid.UUID(r.url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid}/lines", data={"name": "Шаровая (без ячейки)", "qty": "1", "price": "700"})
    client.post(f"/purchases/{pid}/ordered", data={})
    with SessionLocal() as db:
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.purchase_id == pid))
    r = client.post("/stock/receive", data={"line_id": str(line.id), f"inv_{line.id}": "ТТН-1", "no_cell": "1"})
    assert "Без ячейки не принято" in r.text
    with SessionLocal() as db:
        assert not db.get(PurchaseLine, line.id).received
    client.post("/stock/locations", data={"rack": "Я", "shelves": "1", "places": "1"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "Я"))
    client.post("/stock/receive", data={"line_id": str(line.id), f"inv_{line.id}": "ТТН-1", f"loc_{line.id}": str(loc.id)})
    with SessionLocal() as db:
        l = db.get(PurchaseLine, line.id)
        assert l.received and l.location_id == loc.id
    # «Мои закупки» у кладовщика в меню; у запчастиста «Закупок» нет, а «Мои заказы» — с «По накладным»
    assert "Мои закупки" in client.get("/stock").text
    client.post("/logout")
    login(client, "oleg", "oleg123")
    page = client.get("/my-orders").text
    assert "По накладным" in page and "Новая закупка" in page and 'href="/purchases"' in page
    assert ">Закупки<" not in client.get("/stock").text.split("<main", 1)[0]
    client.post("/logout")
    login(client, "irina", "irina123")
    nav = client.get("/stock").text.split("<main", 1)[0]
    # 20.11.2026 (брат): у менеджера своих закупок нет (химию заказывает запчастист) — «Мои закупки» убрано
    assert "Мои закупки" not in nav and "Заказы запчастистов" in nav
    assert "Принять" not in client.get("/purchases/by-partsman").text.split("<main", 1)[1]
    client.post("/logout")