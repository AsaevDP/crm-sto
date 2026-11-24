"""24.11.2026 (брат, наряды 23 и 27): «Пришли запчасти» — одно правило во всей CRM.
Кружок считает НАРЯДЫ (как плитка), мигают ВСЕ детали, которые ждут «Запросить выдачу» (и с полки склада, и приехавшие
по заказу). Зелёный «ждём «✓ Получил»» — только после выдачи. Подпись на плитке наряда — дело того, кто смотрит."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PurchaseLine, User, WoPart
from app.services import orders as osvc
from app.services.notify import arrived_parts

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo


def test_arrived_counts_orders_and_all_parts(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="KA2323AA", phone="0670002323", works="Работа").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": "Прокладка (по заказу)", "qty": "1", "cost": "100", "markup_pct": "50", "action": "order"})
    agree(client, url)
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    # две детали — «с полки» (в резерве без закупки)
    with SessionLocal() as db:
        wo = _wo(url)
        current_branch_id.set(wo.branch_id)
        shelf = [WoPart(work_order_id=wo.id, name=f"С полки {i}", qty=Decimal(1), price=Decimal(100), status="reserved",
                        source="from_stock") for i in (1, 2)]
        db.add_all(shelf)
        db.commit()
        shelf_ids = {str(p.id) for p in shelf}
        irina = db.scalar(select(User).where(User.login == "irina"))
    # пока по заказу ничего не пришло — «Пришли запчасти» нет
    with SessionLocal() as db:
        assert wo.id not in arrived_parts(db, wo.branch_id, irina.id, "ask")
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-23"})
    with SessionLocal() as db:
        ask = arrived_parts(db, wo.branch_id, irina.id, "ask")
        rows = {str(i) for i in arrived_parts(db, wo.branch_id, irina.id, "ask", part_ids=True)}
        label = osvc.stage_label(db, db.get(type(wo), wo.id), viewer=irina)
    assert ask.get(wo.id) == 3                                  # все 3 детали ждут — и 2 с полки
    assert shelf_ids <= rows and str(part.id) in rows
    assert label == "Пришли запчасти — запросить выдачу"

    _as(client, "irina")
    b = client.get("/api/notify").json()["badges"].get("/orders", [])
    red = next(x for x in b if x["c"] == "red" and x["t"].startswith("пришли запчасти"))
    assert red["n"] == len(ask)                                   # цифра = наряды
    assert all(f'.part[data-id="{i}"]' in red["rows"] for i in shelf_ids | {str(part.id)})
    page = client.get("/orders?tab=arrived").text
    assert wo.number in page
