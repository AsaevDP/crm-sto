"""Разбор (11.11.2026): справочник марок/моделей/категорий, дерево на складе, продажи и отправки:
резерв → отправлено (ТТН) → получено = продано; возврат на склад; списание; состояния деталей."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, RazborCategory, RazborMake, RazborModel, StockItem, UsedSale
from app.services import razbor as rsvc
from app.services import stock as ssvc

from .conftest import login


def _bal(item_id):
    with SessionLocal() as db:
        i = db.get(StockItem, item_id)
        return ssvc.balances(db, i.branch_id, [i.id])[i.id]


def _state(item_id):
    with SessionLocal() as db:
        i = db.get(StockItem, item_id)
        return rsvc.item_states(db, i.branch_id, [i])[i.id]


def test_refs_tree_and_sales_flow(client):
    login(client, "dmitro", "dmitro123")
    # Донор новой марки — марка и модель сами появились в справочнике
    client.post("/razbor/donors", data={"make": "Skoda (тест)", "model": "Octavia A7 (тест)", "year": "2016"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == "Skoda (тест)"))
        mk = db.scalar(select(RazborMake).where(RazborMake.name == "Skoda (тест)"))
        assert mk and db.scalar(select(RazborModel).where(RazborModel.make_id == mk.id, RazborModel.name == "Octavia A7 (тест)"))
        assert db.scalar(select(RazborCategory).where(RazborCategory.name == "Оптика", RazborCategory.parent_id.is_(None)))
    for name, price, catsub in (("Фара левая (тест разбора)", "5000", "Оптика||Фары"),
                                ("Зеркало левое (тест разбора)", "1500", "Стёкла и зеркала||"),
                                ("Сиденье водителя (тест разбора)", "3000", "Салон||Сиденья")):
        client.post(f"/razbor/donors/{d.id}/parts", data={"name": name, "price": price, "qty": "1", "condition": "good",
                                                          "catsub": catsub})
    with SessionLocal() as db:
        rows = db.execute(select(StockItem, rsvc.PartCatalog).join(rsvc.PartCatalog, rsvc.PartCatalog.id == StockItem.catalog_id)
                          .where(StockItem.donor_id == d.id)).all()
        by = {c.name: i for i, c in rows}
        cats = {c.name: (c.category, c.subcategory) for i, c in rows}
    assert cats["Фара левая (тест разбора)"] == ("Оптика", "Фары")
    lamp, mirror, seat = by["Фара левая (тест разбора)"], by["Зеркало левое (тест разбора)"], by["Сиденье водителя (тест разбора)"]
    # Дерево: марка → модель → категория → подкатегория
    page = client.get("/razbor?make=Skoda+(тест)&model=Octavia+A7+(тест)&cat=Оптика").text
    assert "Фара левая (тест разбора)" in page and "Зеркало левое (тест разбора)" not in page and "Фары" in page

    # Почта: новая продажа с экрана «Продажи» → резерв → «Отправить» → ТТН → в пути → получено
    client.post("/razbor/sales/new", data={"item_id": str(lamp.id), "action": "reserve", "buyer_name": "Покупатель НП (тест)",
                                           "buyer_phone": "0671112233", "delivery": "np", "np_address": "Киев, отд. 12",
                                           "cod": "1", "price": "5000", "channel": "olx"})
    with SessionLocal() as db:
        s = db.scalar(select(UsedSale).where(UsedSale.stock_item_id == lamp.id))
        assert s.status == "reserved" and s.cod and s.np_address == "Киев, отд. 12" and s.channel == "olx"
    assert _state(lamp.id) == "reserved"
    assert "Покупатель НП (тест)" in client.get("/razbor/sales?tab=ship").text
    r = client.post(f"/razbor/sales/{s.id}", data={"action": "ship", "ttn": "", "back": "/razbor/sales?tab=ship"})
    assert "ТТН" in r.text   # без ТТН Новой Почты не отправить
    client.post(f"/razbor/sales/{s.id}", data={"action": "ship", "ttn": "20450000111222", "cod": "1"})
    with SessionLocal() as db:
        s = db.get(UsedSale, s.id)
        assert s.status == "sent" and s.ttn == "20450000111222" and not s.paid
    assert _bal(lamp.id)["on_hand"] == 0 and _state(lamp.id) == "sent"
    assert "20450000111222" in client.get("/razbor/sales?tab=sent").text
    client.post(f"/razbor/sales/{s.id}", data={"action": "complete", "pay_method": "card"})
    with SessionLocal() as db:
        s = db.get(UsedSale, s.id)
        assert s.status == "sold" and s.paid and s.sold_at and s.pay_method == "card"
    assert _state(lamp.id) == "sold"

    # Зеркало: отправили, не забрали — возврат на склад
    client.post(f"/razbor/parts/{mirror.id}/sell", data={"action": "reserve", "buyer_name": "Не забрал (тест)",
                                                         "delivery": "np", "price": "1500"})
    with SessionLocal() as db:
        s2 = db.scalar(select(UsedSale).where(UsedSale.stock_item_id == mirror.id))
    client.post(f"/razbor/sales/{s2.id}", data={"action": "ship", "ttn": "20450000333444"})
    client.post(f"/razbor/sales/{s2.id}", data={"action": "return", "note": "не забрал с почты"})
    with SessionLocal() as db:
        assert db.get(UsedSale, s2.id).status == "returned"
    assert _bal(mirror.id)["free"] == 1 and _state(mirror.id) == "stock"

    # Сиденье: самовывоз — сразу продано; списать проданное нельзя
    client.post(f"/razbor/parts/{seat.id}/sell", data={"action": "sold", "buyer_name": "Самовывоз (тест)", "price": "3000",
                                                       "delivery": "pickup", "pay_method": "cash"})
    assert _state(seat.id) == "sold"
    client.post(f"/razbor/parts/{mirror.id}/writeoff", data={"qty": "1", "reason": "треснуло при упаковке"})
    assert _state(mirror.id) == "writeoff"

    # Справочник: модель с деталями не удалить, пустую марку — можно
    with SessionLocal() as db:
        md = db.scalar(select(RazborModel).where(RazborModel.name == "Octavia A7 (тест)"))
    client.post("/razbor/refs", data={"action": "del_model", "rid": str(md.id)})
    with SessionLocal() as db:
        assert db.get(RazborModel, md.id) is not None
    client.post("/razbor/refs", data={"action": "add_make", "name": "Пустая марка (тест)"})
    with SessionLocal() as db:
        empty = db.scalar(select(RazborMake).where(RazborMake.name == "Пустая марка (тест)"))
    client.post("/razbor/refs", data={"action": "del_make", "rid": str(empty.id)})
    with SessionLocal() as db:
        assert db.get(RazborMake, empty.id) is None
    # Переименовать подкатегорию — у детали тоже
    with SessionLocal() as db:
        headlights = db.scalar(select(RazborCategory).where(RazborCategory.name == "Фары"))
    client.post("/razbor/refs", data={"action": "rename_cat", "rid": str(headlights.id), "name": "Фары головные"})
    with SessionLocal() as db:
        assert db.get(rsvc.PartCatalog, db.get(StockItem, lamp.id).catalog_id).subcategory == "Фары головные"
    for u in ("/razbor", "/razbor/sales", "/razbor/refs", "/razbor?f=out", f"/razbor/parts/{lamp.id}", f"/razbor/donors/{d.id}"):
        assert client.get(u).status_code == 200, u
    client.post("/logout")
