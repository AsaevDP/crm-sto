"""Шаг 5: склад. Критерий брифа: купили 5 → 1 в наряд, 4 на склад → следующий наряд берёт со склада, остатки сходятся."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Location, PartCatalog, Purchase, PurchaseLine, StockItem, WoPart, WorkOrder
from app.services import settings as st
from app.services.stock import balances

from .conftest import agree, login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _uid_oleg():
    from app.models import User
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == "oleg"))


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _bal(name):
    with SessionLocal() as db:
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == name))
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id)).all()
        b = balances(db, items[0].branch_id, [i.id for i in items])
        return tuple(sum((b[i.id][k] for i in items), Decimal(0)) for k in ("on_hand", "reserved", "free"))


def test_buy5_one_to_order_four_to_stock_next_order_from_stock(client):
    name = "Колодки передние TRW (тест склада)"
    # Наряд 1: сценарий А сразу в подбор
    login(client, "irina", "irina123")
    url1 = _create(client, scenario="A", photos=1, plate="ВВ 5001 ВВ", phone="0505001000").json()["url"]
    client.post("/logout")
    assert _wo(url1).status == "approve"   # 13.11.2026: подбор и согласование одновременно

    # Запчастист: новая позиция, закупка 100, наценка 100 % → цена 200; заказываем 5 шт (1 в наряд, 4 на склад)
    login(client, "oleg", "oleg123")
    client.post(f"{url1}/parts", data={"name": name, "article": "GDB1234", "brand": "TRW", "qty": "1", "cost": "100",
                                       "markup_pct": "100", "action": "order", "order_qty": "5"})
    with SessionLocal() as db:
        part1 = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url1).id))
        assert (part1.price, part1.status, part1.plan) == (Decimal("200.00"), "need", "order")   # до согласования не заказана
        assert db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == part1.id)) is None
    agree(client, url1)
    assert _wo(url1).status == "waiting"
    with SessionLocal() as db:
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == part1.id))
        assert line.qty == 5 and db.get(Purchase, line.purchase_id).created_by == _uid_oleg()
        pid = line.purchase_id
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    client.post("/logout")

    # Кладовщик: стеллаж, приход в ячейку → 1 сразу в резерв под наряд
    login(client, "pavel", "pavel123")
    client.post("/stock/locations", data={"rack": "B", "shelves": "1", "places": "2"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.label == "B / 1 / 1"))
    client.post(f"/purchases/{pid}/receive", data={"location_id": str(loc.id)})
    assert _bal(name) == (5, 1, 4)
    assert _wo(url1).status == "instock"
    with SessionLocal() as db:
        assert db.get(Purchase, pid).status == "arrived"
    client.post("/logout")

    # Слесарь просит выдачу → у кладовщика окошко → выдал → наряд «В работе»
    login(client, "andrey", "andrey123")
    page = client.get(url1).text
    assert "GDB1234" not in page and "закупка" not in page  # артикул и закупка слесарю не видны
    client.post(f"{url1}/issue-request", data={"comment": "срочно"})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    n = client.get("/api/notify", headers=FETCH).json()
    assert n["issues"] == 1 and "Андрей" in n["items"][0]["who"]
    with SessionLocal() as db:
        from app.models import IssueRequest
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.status == "new"))
    client.post(f"/stock/issues/{rid}/issue")
    assert _bal(name) == (4, 0, 4)
    assert _wo(url1).status == "work"
    client.post("/logout")

    # Наряд 2: та же деталь — со склада, без закупки
    login(client, "irina", "irina123")
    url2 = _create(client, scenario="A", photos=1, plate="ВВ 5002 ВВ", phone="0505002000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    with SessionLocal() as db:
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == name))
        n_lines_before = len(db.scalars(select(PurchaseLine)).all())
    found = client.get("/api/catalog/search?q=GDB1234", headers=FETCH).json()["items"]
    assert found[0]["free"] == 4
    client.post(f"{url2}/parts", data={"catalog_id": str(cat.id), "qty": "1", "cost": "100", "markup_pct": "50"})
    assert _bal(name) == (4, 1, 3)   # со склада — в резерв сразу, ещё на согласовании
    agree(client, url2)
    assert _wo(url2).status == "instock"
    assert _bal(name) == (4, 1, 3)
    with SessionLocal() as db:
        assert len(db.scalars(select(PurchaseLine)).all()) == n_lines_before  # новой закупки не было
        p2 = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url2).id))
        assert (p2.source, p2.price) == ("from_stock", Decimal("150.00"))

    # Наценка меняется по позиции → цена пересчитывается
    r = client.post(f"{url2}/parts/{p2.id}/markup", data={"markup_pct": "30"}, headers=FETCH).json()
    assert r["price"] == "130.00"


def test_inventory_adjust_requires_reason(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="ВВ 5003 ВВ", phone="0505003000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Фильтр масляный (инв)", "qty": "1", "cost": "50", "action": "order", "order_qty": "3"})
    agree(client, url)
    with SessionLocal() as db:
        pid = db.scalar(select(PurchaseLine.purchase_id).join(WoPart, WoPart.id == PurchaseLine.wo_part_id)
                        .where(WoPart.name == "Фильтр масляный (инв)"))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    client.post(f"/purchases/{pid}/receive", data={})
    with SessionLocal() as db:
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == "Фильтр масляный (инв)"))
        item = db.scalar(select(StockItem).where(StockItem.catalog_id == cat.id))
    client.post(f"/stock/items/{item.id}/adjust", data={"actual": "1", "reason": ""})
    assert _bal("Фильтр масляный (инв)")[0] == 3
    client.post(f"/stock/items/{item.id}/adjust", data={"actual": "2", "reason": "Пересчёт"})
    assert _bal("Фильтр масляный (инв)") == (2, 1, 1)


def test_partsman_pct_only_admin_and_capped(client):
    login(client, "admin", "admin123")
    client.post("/settings/pricing", data={"default_markup": "40", "hour_rate": "900"})
    client.post("/settings/payroll/defaults", data={"mechanic_works_pct": "30", "partsman_margin_pct": "45"})
    with SessionLocal() as db:
        from app.models import Branch
        b = db.scalar(select(Branch))
        assert st.get(db, b.id, "payroll")["partsman_margin_pct"] == 30  # максимум 30 %
        assert st.get(db, b.id, "pricing")["default_markup"] == 40
        assert st.get(db, b.id, "pricing")["hour_rate"] == 900
    client.post("/settings/pricing", data={"default_markup": "25", "hour_rate": "800"})
    client.post("/settings/payroll/defaults", data={"mechanic_works_pct": "30", "partsman_margin_pct": "7"})
    client.post("/logout")
    login(client, "irina", "irina123")  # менеджер не может менять проценты
    assert client.post("/settings/payroll/defaults", data={"partsman_margin_pct": "20"}).status_code == 403
