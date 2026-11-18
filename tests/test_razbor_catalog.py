"""Категории разбора по подробному справочнику (18.11.2026, брат): 20 категорий с подкатегориями заведены сами (и пустые),
категория угадывается по названию (с подкатегорией), в форме — выбор категории и подсказки, количество у категорий,
свои категории и подкатегории можно добавлять."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, PartCatalog, RazborCategory, StockItem
from app.services.razbor_catalog_data import CATALOG

from .conftest import login


def test_catalog_categories_and_guess(client):
    with SessionLocal() as db:
        top = {c.name for c in db.scalars(select(RazborCategory).where(RazborCategory.parent_id.is_(None)))}
        assert {c for c, _ in CATALOG} <= top
        light = db.scalar(select(RazborCategory).where(RazborCategory.name == "Освещение", RazborCategory.parent_id.is_(None)))
        subs = {s.name for s in db.scalars(select(RazborCategory).where(RazborCategory.parent_id == light.id))}
        assert {"Передняя оптика", "Задняя оптика"} <= subs
    login(client, "dmitro", "dmitro123")
    client.post("/razbor/donors", data={"make": "Ford (справочник)", "model": "Focus III", "year": "2014"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == "Ford (справочник)"))
    # без категории — угадали вместе с подкатегорией; с выбранной — как выбрали
    client.post(f"/razbor/donors/{d.id}/parts", data={"name": "Фонарь задний левый (спр)", "price": "1500", "catsub": ""})
    client.post(f"/razbor/donors/{d.id}/parts", data={"name": "Блок ABS (спр)", "price": "3000", "catsub": ""})
    client.post(f"/razbor/donors/{d.id}/parts", data={"name": "Деталь (спр)", "price": "500",
                                                      "catsub": "Кузов — наружные панели||Двери"})
    with SessionLocal() as db:
        got = {c.name: (c.category, c.subcategory) for c in db.scalars(
            select(PartCatalog).join(StockItem, StockItem.catalog_id == PartCatalog.id).where(StockItem.donor_id == d.id))}
    assert got["Фонарь задний левый (спр)"] == ("Освещение", "Задняя оптика")
    assert got["Блок ABS (спр)"] == ("Тормозная система", "Электроника")
    assert got["Деталь (спр)"] == ("Кузов — наружные панели", "Двери")
    assert client.get("/api/razbor/guess?name=Капот").json()["catsub"] == "Кузов — наружные панели||Передняя часть"
    # форма: выбор категории и подсказки; список — все категории с количеством
    page = client.get(f"/razbor/donors/{d.id}").text
    assert 'id="dn-addpart"' in page and 'id="rz-hints"' in page and "Освещение › Задняя оптика" in page
    lst = client.get("/razbor?v=parts&empty=1").text   # 19.11.2026: «Все детали», с пустыми
    assert "Гибрид и электро" in lst and "Освещение" in lst
    # свою категорию и подкатегорию добавить можно
    client.post("/razbor/refs", data={"action": "add_cat", "name": "Тюнинг (спр)"})
    with SessionLocal() as db:
        t = db.scalar(select(RazborCategory).where(RazborCategory.name == "Тюнинг (спр)"))
    client.post("/razbor/refs", data={"action": "add_sub", "rid": str(t.id), "name": "Обвес (спр)"})
    with SessionLocal() as db:
        assert db.scalar(select(RazborCategory).where(RazborCategory.parent_id == t.id, RazborCategory.name == "Обвес (спр)"))
    client.post("/logout")
