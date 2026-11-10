"""Закупки (11.11.2026): несколько черновиков, вставка списка, правка и перенос строк, приход частями,
резерв под наряд досчитывается, когда пришло всё."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Purchase, PurchaseLine, Supplier, WoPart, WorkOrder
from app.services import stock as svc

from .conftest import login
from .test_step3 import _create


def _p(pid):
    with SessionLocal() as db:
        p = db.get(Purchase, pid)
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == pid).order_by(PurchaseLine.created_at)).all()
        return p, lines


def test_parse_paste():
    rows = svc.parse_paste("Наименование\tАртикул\tБренд\tКол-во\tЦена\n"
                           "Колодки передние\tGDB1550\tTRW\t2\t1450\n"
                           "Фильтр масляный; W712/75; MANN; 4; 260,50\n"
                           "\n"
                           "Свеча зажигания | BKR6EK | NGK | 8 | 180\n"
                           "Просто название")
    assert [r["name"] for r in rows] == ["Колодки передние", "Фильтр масляный", "Свеча зажигания", "Просто название"]
    assert rows[0]["article"] == "GDB1550" and rows[0]["brand"] == "TRW" and rows[0]["qty"] == 2 and rows[0]["price"] == Decimal("1450.00")
    assert rows[1]["price"] == Decimal("260.50") and rows[1]["qty"] == 4
    assert rows[3]["qty"] == 1 and rows[3]["price"] == 0


def test_purchase_flow_partial_receive(client):
    login(client, "oleg", "oleg123")
    # Две закупки у разных поставщиков одновременно
    r1 = client.post("/purchases/new", data={"supplier_new": "Поставщик А (тест)"})
    r2 = client.post("/purchases/new", data={"supplier_new": "Поставщик Б (тест)"})
    pa = uuid.UUID(r1.url.path.rsplit("/", 1)[1])
    pb = uuid.UUID(r2.url.path.rsplit("/", 1)[1])
    assert pa != pb
    # Вставка списка в А
    client.post(f"/purchases/{pa}/lines", data={"paste": "Колодки (тест закупки)\tGDB9001\tTRW\t4\t1000\n"
                                                         "Диск (тест закупки);DF9002;TRW;2;2000"})
    client.post(f"/purchases/{pa}/lines", data={"name": "Лампа (тест закупки)", "qty": "10", "price": "50"})
    p, lines = _p(pa)
    assert len(lines) == 3 and p.status == "draft"
    lamp = next(l for l in lines if l.qty == 10)
    # Лампу переносим в Б, диски — правим количество
    client.post(f"/purchases/{pa}/lines/{lamp.id}", data={"action": "move", "target": str(pb)})
    disc = next(l for l in lines if l.qty == 2)
    client.post(f"/purchases/{pa}/lines/{disc.id}", data={"action": "save", "qty": "3", "price": "1900"})
    p, lines = _p(pa)
    assert len(lines) == 2 and any(l.qty == 3 and l.price == Decimal("1900.00") for l in lines)
    assert len(_p(pb)[1]) == 1
    # Удалить черновик Б
    client.post(f"/purchases/{pb}", data={"action": "delete"})
    with SessionLocal() as db:
        assert db.get(Purchase, pb) is None
    client.post(f"/purchases/{pa}", data={"action": "ordered", "expected_on": "2026-11-20"})
    p, lines = _p(pa)
    assert p.status == "ordered" and p.ordered_on and str(p.expected_on) == "2026-11-20"
    client.post("/logout")

    # Кладовщик: колодок пришло 1 из 4 → закупка «пришла частично», строка ещё ждёт
    login(client, "pavel", "pavel123")
    pads = next(l for l in lines if l.qty == 4)
    client.post("/stock/receive", data={"line_id": str(pads.id), f"qty_{pads.id}": "1"})
    p, lines = _p(pa)
    pads = next(l for l in lines if l.id == pads.id)
    assert p.status == "partial" and pads.got == 1 and not pads.received and pads.left == 3
    assert "пришло 1 из 4" in client.get("/stock/receive").text
    client.post("/logout")

    # Запчастист: остаток колодок поставщик не привезёт — переносим в новый черновик
    login(client, "oleg", "oleg123")
    client.post(f"/purchases/{pa}/lines/{pads.id}", data={"action": "rest", "target": "new"})
    p, lines = _p(pa)
    pads = next(l for l in lines if l.id == pads.id)
    assert pads.qty == 1 and pads.received
    with SessionLocal() as db:
        rest = db.scalar(select(PurchaseLine).where(PurchaseLine.catalog_id == pads.catalog_id, PurchaseLine.received.is_(False)))
        assert rest.qty == 3 and db.get(Purchase, rest.purchase_id).status == "draft"
    client.post("/logout")

    # Принять всё остальное → закупка «пришла»
    login(client, "pavel", "pavel123")
    client.post(f"/purchases/{pa}/receive", data={"all": str(pa)})
    assert _p(pa)[0].status == "arrived"
    client.post("/logout")


def test_order_part_partial_arrival_reserves_when_all_came(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 8101 КА", phone="0508101000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Сайлентблок (тест частями)", "qty": "4", "cost": "100", "markup_pct": "30", "action": "order"})
    client.post(f"{url}/picking-done")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == wo.id))
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == part.id))
    # Меньше, чем нужно наряду, поставить нельзя
    client.post(f"/purchases/{line.purchase_id}/lines/{line.id}", data={"action": "save", "qty": "2", "price": "100"})
    assert _p(line.purchase_id)[1][-1].qty >= 4
    client.post(f"/purchases/{line.purchase_id}", data={"action": "ordered"})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    client.post("/stock/receive", data={"line_id": str(line.id), f"qty_{line.id}": "3"})
    with SessionLocal() as db:
        part = db.get(WoPart, part.id)
        assert part.status == "ordered" and sum(svc.reserved_for_part(db, part.id).values()) == 3
        assert db.get(WorkOrder, wo.id).status == "waiting"
    client.post("/stock/receive", data={"line_id": str(line.id)})
    with SessionLocal() as db:
        part = db.get(WoPart, part.id)
        assert part.status == "reserved" and sum(svc.reserved_for_part(db, part.id).values()) == 4
        assert db.get(WorkOrder, wo.id).status == "instock"
    client.post("/logout")
