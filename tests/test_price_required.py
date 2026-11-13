"""Цена обязательна (13.11.2026, брат): без цены у запчасти запчастист не завершит подбор; деталь со склада берёт
закупку со склада; на склад ничего не принимается без цены из накладной."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PartCatalog, PurchaseLine, StockItem, WoPart

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo

FETCH = {"x-requested-with": "fetch"}


def _part(url, name):
    with SessionLocal() as db:
        return db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id, WoPart.name == name))


def test_price_required_for_picking_and_receiving(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="ЦН 1301 ЦН", phone="0509301000", works="Подвеска").json()["url"]
    _as(client, "oleg")
    # Новая деталь без закупки → цены нет → «Подбор завершён» не нажать
    client.post(f"{url}/parts", data={"name": "Стойка (без цены)", "qty": "1", "cost": "", "markup_pct": "30", "action": "order"})
    assert _part(url, "Стойка (без цены)").price == 0
    r = client.post(f"{url}/picking-done")
    assert "Укажите цену" in r.text and _wo(url).picked_at is None
    # Вписал закупку → цена есть → подбор завершён
    p = _part(url, "Стойка (без цены)")
    client.post(f"{url}/parts/{p.id}/markup", data={"cost": "1000", "markup_pct": "30"}, headers=FETCH)
    assert _part(url, "Стойка (без цены)").price == Decimal("1300.00")
    agree(client, url, then="pavel")
    assert _wo(url).status == "waiting"

    # Кладовщик: в закупке у строки нет цены (поставщик не назвал) → не принимается, пока не впишет из накладной
    from app.db import current_branch_id
    with SessionLocal() as db:
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == p.id))
        current_branch_id.set(line.branch_id)
        line.price = Decimal(0)
        db.commit()
        pid, lid = line.purchase_id, line.id
    _as(client, "oleg")
    client.post(f"/purchases/{pid}", data={"action": "save", "supplier_new": "Цена-поставщик"})   # без поставщика не заказать
    client.post(f"/purchases/{pid}/ordered", data={})
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/delivered", data={})
    r = client.post(f"/purchases/{pid}/receive", data={})
    assert "Без цены не принято" in r.text
    with SessionLocal() as db:
        assert not db.get(PurchaseLine, lid).received
    page = client.get("/stock/receive").text
    assert f'name="price_{lid}"' in page                              # поле «₴ за шт» у кладовщика
    client.post("/stock/receive", data={"line_id": str(lid), f"price_{lid}": "980"})
    with SessionLocal() as db:
        l = db.get(PurchaseLine, lid)
        assert l.received and l.price == Decimal("980.00")
        item = db.scalar(select(StockItem).where(StockItem.catalog_id == l.catalog_id))
        assert item.cost == Decimal("980.00")

    # Деталь со склада без закупки в форме — закупка берётся со склада, цена не 0
    _as(client, "irina")
    url2 = _create(client, scenario="A", photos=1, plate="ЦН 1302 ЦН", phone="0509302000", works="ТО").json()["url"]
    _as(client, "oleg")
    with SessionLocal() as db:
        cat = db.scalar(select(PartCatalog).where(PartCatalog.id == item.catalog_id))
    client.post(f"{url2}/parts", data={"catalog_id": str(cat.id), "name": cat.name, "qty": "1", "cost": "", "markup_pct": "20",
                                       "action": "order"})
    p2 = _part(url2, cat.name)
    assert p2.cost == Decimal("980.00") and p2.price == Decimal("1176.00")
