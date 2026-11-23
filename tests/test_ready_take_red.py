"""24.11.2026 (брат, наряд 27): менеджеру «Забрать у окошка» — 🔴 (его дело), «Ждут кладовщика» — 🟣 для сведения.
В наряде мигают ВСЕ детали подготовленного запроса — и взятые с полки склада, не только приехавшие по закупке
(раньше из 10 деталей мигали 4)."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import IssueRequestLine, WoPart

from .test_issue_menu import _reserved_order
from .test_step6 import _as, _wo


def _ready_badges(client):
    return client.get("/api/notify").json()["badges"].get("/ready", [])


def test_take_red_wait_purple_all_parts_blink(client):
    url, rid = _reserved_order(client, "KA2701AA", "0670002701", "Фильтр масляный (по заказу)")
    # вторая деталь — как будто взята с полки склада (без закупки), в том же запросе на выдачу
    with SessionLocal() as db:
        wo = _wo(url)
        current_branch_id.set(wo.branch_id)
        shelf = WoPart(work_order_id=wo.id, name="Фильтр салона (с полки)", qty=Decimal(1), price=Decimal(300),
                       status="reserved", source="from_stock")
        db.add(shelf)
        db.flush()
        db.add(IssueRequestLine(request_id=rid, wo_part_id=shelf.id, qty=Decimal(1)))
        db.commit()
        all_ids = {str(i) for i in db.scalars(select(IssueRequestLine.wo_part_id).where(IssueRequestLine.request_id == rid))}
    assert len(all_ids) == 2

    # Ждёт кладовщика: у менеджера сиреневая «для сведения», красной «забрать» нет
    _as(client, "irina")
    b = _ready_badges(client)
    assert any(x["c"] == "purple" and x["info"] and "ждут кладовщика" in x["t"] for x in b)
    wo_id = str(_wo(url).id)
    assert not any("забрать у окошка" in x["t"] and wo_id in x["wo"] for x in b)   # другие тесты могли оставить свои
    page = client.get("/ready").text
    assert "rim-purple" in page

    # Кладовщик подготовил → у менеджера 🔴, в наряде мигают обе детали
    _as(client, "pavel")
    client.post(f"/stock/issues/{rid}/ready")
    _as(client, "irina")
    b = _ready_badges(client)
    take = next(x for x in b if x["c"] == "red" and "забрать у окошка" in x["t"])
    assert str(_wo(url).id) in take["wo"]
    for i in all_ids:
        assert f'.part[data-id="{i}"]' in take["rows"]
    assert not any(x["c"] == "orange" for x in b)
    # зелёный «Пришли запчасти — выдаются» в наряде тоже ведёт ко всем деталям запроса
    going = [x for x in client.get("/api/notify").json()["badges"].get("/orders", [])
             if x["c"] == "green" and "выдаются" in x["t"]]
    assert going and all(f'.part[data-id="{i}"]' in going[0]["rows"] for i in all_ids)
    # в наряде у обеих деталей есть data-id — им есть что подсветить
    order = client.get(url).text
    for i in all_ids:
        assert f'data-id="{i}"' in order
