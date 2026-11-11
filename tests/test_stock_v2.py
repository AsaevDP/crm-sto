"""Склад (10.11.2026): ТО / Запчасти без б/у, дерево категорий, «подходит для», штрихкод, наклейки, печать, Excel."""
import io

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PartCatalog, StockCategory, StockItem
from app.services import barcode as bc

from .test_step6 import _as


def test_code128_table():
    assert len(bc._P) == 107 and all(sum(map(int, p)) == 11 for p in bc._P[:106]) and sum(map(int, bc._P[106])) == 13
    assert len(set(bc._P)) == 107
    assert bc.encode("200000000017")[0] == bc.START_C and bc.encode("Р-1")[0] == bc.START_B
    assert "<svg" in bc.svg("200000000017")


def _item(name):
    with SessionLocal() as db:
        c = db.scalar(select(PartCatalog).where(PartCatalog.name == name))
        return db.scalar(select(StockItem).where(StockItem.catalog_id == c.id))


def test_stock_tree_fits_barcode_labels(client):
    _as(client, "pavel")
    client.post("/stock/locations", data={"rack": "Z", "shelves": "1", "places": "1"})
    # Приход через закупку проще заменить прямым созданием позиции через сервис
    from app.db import current_branch_id
    from app.models import Branch
    from app.services import stock as svc
    with SessionLocal() as db:
        b = db.scalar(select(Branch))
        current_branch_id.set(b.id)
        c = svc.get_or_create_catalog(db, "Колодки (тест склада)", "TST-1", "TRW")
        c.section, c.category = "repair", "Тормоза"
        i = svc.get_or_create_item(db, c.id, None)
        svc.move(db, "in", i, 3, comment="тест")
        db.commit()
    it = _item("Колодки (тест склада)")
    assert it.barcode and len(it.barcode) == 12 and it.barcode.startswith("2")   # штрихкод сам

    # Дерево категорий создано, вкладки ТО / Запчасти, б/у нет
    page = client.get("/stock?sec=repair").text
    assert "Колодки (тест склада)" in page and "Б/у" not in page
    assert '<span class="ptile-t">ТО</span>' in page and '<span class="ptile-t">Запчасти</span>' in page   # плитки разделов
    with SessionLocal() as db:
        assert db.scalar(select(StockCategory).where(StockCategory.name == "Тормоза"))
        sub = db.scalar(select(StockCategory).where(StockCategory.name == "Колодки"))
        brakes = db.scalar(select(StockCategory).where(StockCategory.name == "Тормоза", StockCategory.parent_id.is_(None)))

    # Категория + подкатегория, «подходит для», фильтр по марке/модели
    client.post(f"/stock/items/{it.id}/category", data={"section": "repair", "category": "Тормоза", "subcategory": "Колодки"})
    client.post(f"/stock/items/{it.id}/fits", data={"make": "Renault", "model": "Megane IV", "year_from": "2016"})
    assert "Колодки (тест склада)" in client.get("/stock?sec=repair&make=Renault&model=Megane IV").text
    assert "Колодки (тест склада)" not in client.get("/stock?sec=repair&make=Toyota").text
    assert "Колодки (тест склада)" in client.get("/stock?sec=repair&cat=Тормоза&sub=Колодки").text

    # Сканер: штрихкод в поиске → карточка позиции; в подборе наряда — эта деталь
    r = client.get(f"/stock?q={it.barcode}", follow_redirects=False)
    assert r.status_code == 303 and f"/stock/items/{it.id}" in r.headers["location"]
    assert client.get(f"/api/catalog/search?q={it.barcode}").json()["items"][0]["name"] == "Колодки (тест склада)"

    # Переименование категории меняет и запчасть; удалить непустую нельзя
    client.post(f"/stock/categories/{sub.id}", data={"action": "rename", "name": "Колодки тормозные"})
    with SessionLocal() as db:
        assert db.scalar(select(PartCatalog).where(PartCatalog.name == "Колодки (тест склада)")).subcategory == "Колодки тормозные"
    client.post(f"/stock/categories/{brakes.id}", data={"action": "delete"})
    with SessionLocal() as db:
        assert db.get(StockCategory, brakes.id) is not None
    client.post("/stock/categories", data={"section": "to", "name": "Аксессуары (тест)"})
    assert "Аксессуары (тест)" in client.get("/stock/categories").text

    # Наклейки, печать списка, Excel
    lab = client.get(f"/stock/labels?ids={it.id}&copies=2").text
    assert lab.count('class="lab"') == 2 and it.barcode in lab and "@page { size: 100mm 70mm" in lab
    assert "СКЛАД — СПИСОК ПОЗИЦИЙ" in client.get("/stock/print?sec=repair").text
    x = client.get("/stock/export.xlsx?sec=repair")
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(x.content)).active
    assert ws["A1"].value == "Штрихкод" and any(row[1].value == "Колодки (тест склада)" for row in ws.iter_rows(min_row=2))

    # Настройки наклеек: размер и режим
    _as(client, "admin")
    client.post("/settings/labels", data={"size": "58x40", "mode": "sheet", "show_price": "1"})
    lab = client.get(f"/stock/labels?ids={it.id}").text
    assert "width: 58mm" in lab and "@page { size: A4" in lab
