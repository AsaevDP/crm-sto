"""Машина-донор как «Пульт» (11.11.2026): плитки категорий, детали списком, перенос, удаление ошибочной,
категория добавляется/убирается прямо с карточки и видна у всех машин."""
from urllib.parse import quote

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, PartCatalog, RazborCategory, StockItem

from .conftest import login


def _parts(did):
    with SessionLocal() as db:
        return {c.name: (i, c) for i, c in db.execute(select(StockItem, PartCatalog).join(
            PartCatalog, PartCatalog.id == StockItem.catalog_id).where(StockItem.donor_id == did))}


def test_donor_tiles(client):
    login(client, "dmitro", "dmitro123")
    client.post("/razbor/donors", data={"make": "Mazda (тест плиток)", "model": "6 GJ", "year": "2015"})
    client.post("/razbor/donors", data={"make": "Mazda (тест плиток)", "model": "CX-5", "year": "2017"})
    with SessionLocal() as db:
        d1, d2 = db.scalars(select(Donor).where(Donor.make == "Mazda (тест плиток)").order_by(Donor.model)).all()
    # Списком в «Оптика»
    r = client.post(f"/razbor/donors/{d1.id}/parts/bulk", data={"category": "Оптика", "subcategory": "Фары",
                                                                "lines": "Фара левая (плитки); 6500\nФара правая (плитки); 6400\nПТФ левая (плитки)"})
    assert "cat=" in str(r.url)
    p = _parts(d1.id)
    assert {"Фара левая (плитки)", "Фара правая (плитки)", "ПТФ левая (плитки)"} <= set(p)
    assert p["Фара левая (плитки)"][1].category == "Оптика" and p["Фара левая (плитки)"][1].subcategory == "Фары"
    page = client.get(f"/razbor/donors/{d1.id}?cat={quote('Оптика')}").text
    assert "Фара правая (плитки)" in page and "Добавить несколько списком" in page
    # Перенос ПТФ в «Электрика и блоки»
    ptf = p["ПТФ левая (плитки)"][0]
    client.post(f"/razbor/parts/{ptf.id}/move", data={"catsub": "Электрика и блоки||", "back": f"/razbor/donors/{d1.id}"})
    assert _parts(d1.id)["ПТФ левая (плитки)"][1].category == "Электрика и блоки"
    # Новая категория с карточки — видна и у второй машины; пустую можно убрать
    client.post("/razbor/refs", data={"action": "add_cat", "name": "Шины (тест плиток)", "ret": f"/razbor/donors/{d1.id}?empty=1"})
    assert "Шины (тест плиток)" in client.get(f"/razbor/donors/{d2.id}?empty=1").text
    with SessionLocal() as db:
        c = db.scalar(select(RazborCategory).where(RazborCategory.name == "Шины (тест плиток)"))
    client.post("/razbor/refs", data={"action": "del_cat", "rid": str(c.id), "ret": f"/razbor/donors/{d1.id}"})
    with SessionLocal() as db:
        assert db.get(RazborCategory, c.id) is None
    # Удалить ошибочную деталь можно; проданную — нельзя
    left = p["Фара левая (плитки)"][0]
    right = p["Фара правая (плитки)"][0]
    client.post(f"/razbor/parts/{right.id}/sell", data={"action": "sold", "price": "6400", "delivery": "pickup", "pay_method": "cash"})
    client.post(f"/razbor/parts/{right.id}/delete", data={"back": f"/razbor/donors/{d1.id}"})
    client.post(f"/razbor/parts/{left.id}/delete", data={"back": f"/razbor/donors/{d1.id}"})
    after = _parts(d1.id)
    assert "Фара левая (плитки)" not in after and "Фара правая (плитки)" in after
    client.post("/logout")


def test_donor_payback_tiles_group_same_cars(client):
    """«Доноры и окупаемость» плитками (11.11.2026): две одинаковые машины (марка + модель + год) — одна плитка «×2»."""
    login(client, "dmitro", "dmitro123")
    for year in ("2012", "2012", "2014"):
        client.post("/razbor/donors", data={"make": "Opel (двойня)", "model": "Astra J", "year": year})
    page = client.get("/razbor").text
    sec = page.split('class="dt-sec"')[1].split("</section>")[0]
    tiles = [t for t in sec.split('dt-tile') if "Opel (двойня) Astra J" in t]
    assert len(tiles) == 2                                   # 2012 ×2 и 2014 отдельно
    twin = next(t for t in tiles if "×2" in t)
    with SessionLocal() as db:
        codes = [d.code for d in db.scalars(select(Donor).where(Donor.make == "Opel (двойня)", Donor.year == 2012))]
    assert all(c in twin for c in codes) and "2012" in twin
    assert "Новый донор" in sec