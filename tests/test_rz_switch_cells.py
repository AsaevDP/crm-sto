"""26.11.2026 (брат): «Разбор» — крупный переключатель «Машины / Все детали / Продажи и отправки» (у всех, у кого есть
«Разбор»); «Ячейки склада» — у ячейки число позиций, нажал — список, что лежит."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Location, StockItem

from .conftest import login


def test_switch_everywhere(client):
    for who in ("dmitro", "director", "oleg"):
        client.post("/logout")
        login(client, who, who + "123")
        for url in ("/razbor?v=parts", "/razbor/sales"):
            r = client.get(url)
            if r.status_code == 200:
                assert "rz-switch" in r.text and "Продажи и отправки" in r.text and "Все детали" in r.text, (who, url)


def test_cells_show_contents(client):
    from .test_refuse_parts import _stock_item
    cat = _stock_item(client, "Ремень тест-ячейка", qty="2")
    client.post("/logout")
    login(client, "pavel", "pavel123")
    client.post("/stock/locations", data={"rack": "ЯЧ", "shelf": "1", "place": "1"})
    with SessionLocal() as db:
        from app.db import current_branch_id
        loc = db.scalar(select(Location).where(Location.rack == "ЯЧ"))
        it = db.scalar(select(StockItem).where(StockItem.catalog_id == cat.id))
        current_branch_id.set(it.branch_id)
        it.location_id = loc.id
        db.commit()
        lid = loc.id
    page = client.get("/stock/locations").text
    assert f"/stock/locations?c={lid}" in page
    one = client.get(f"/stock/locations?c={lid}").text
    assert 'id="cell"' in one and "Лежит" in one and "Ремень тест-ячейка" in one
