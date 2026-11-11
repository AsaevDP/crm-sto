"""Разбор → «Выдача и отправка» (12.11.2026): подготовить → готово к выдаче / к отправке → выдано / отправлено;
службы доставки (почта с ТТН, курьер без ТТН), наша наклейка, таблица посылок, реестр отправок за день."""
import re
from datetime import date

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Carrier, Donor, StockItem, UsedSale
from app.services import razbor as rsvc

from .conftest import login


def _sale(item_id):
    with SessionLocal() as db:
        return db.scalar(select(UsedSale).where(UsedSale.stock_item_id == item_id).order_by(UsedSale.created_at.desc()))


def test_razbor_shipping(client):
    login(client, "dmitro", "dmitro123")
    client.post("/razbor/donors", data={"make": "Ford (отправки)", "model": "Focus (отправки)", "year": "2014"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == "Ford (отправки)"))
    for name in ("Фара (самовывоз)", "Бампер (почта)", "Дверь (курьер)"):
        client.post(f"/razbor/donors/{d.id}/parts", data={"name": name, "price": "1000", "qty": "1", "condition": "good",
                                                          "catsub": "Оптика||Фары"})
    with SessionLocal() as db:
        items = {db.get(rsvc.PartCatalog, i.catalog_id).name: i for i in db.scalars(select(StockItem).where(StockItem.donor_id == d.id))}
    before = client.get("/api/notify").json()["razbor"]
    # Служба «курьер» — без ТТН
    client.post("/razbor/carriers", data={"name": "Курьер Саша (тест)", "kind": "courier", "phone": "0670000001"})
    assert "Нова Пошта" in client.get("/razbor/carriers").text
    with SessionLocal() as db:
        courier = db.scalar(select(Carrier).where(Carrier.name == "Курьер Саша (тест)"))
        np = db.scalar(select(Carrier).where(Carrier.name == "Нова Пошта"))
    for key, delivery, extra in (("Фара (самовывоз)", "pickup", {}), ("Бампер (почта)", "np", {"np_address": "Львов, отд. 5", "cod": "1"}),
                                 ("Дверь (курьер)", "other", {"np_address": "Киев, ул. Тестовая 1"})):
        client.post("/razbor/sales/new", data={"item_id": str(items[key].id), "action": "reserve", "buyer_name": f"Покупатель {key}",
                                               "buyer_phone": "0671234567", "delivery": delivery, "price": "1000", **extra})
    assert client.get("/api/notify").json()["razbor"] == before + 3
    page = client.get("/razbor/shipping?st=prepare").text
    assert "Покупатель Фара (самовывоз)" in page and "🙋 Готово к выдаче" in page and "📦 Готово к отправке" in page
    assert 'href="/razbor/shipping"' in page   # пункт меню у кладовщика разбора

    # Самовывоз: упаковал → готово к выдаче → «Выдано» → продано
    s1 = _sale(items["Фара (самовывоз)"].id)
    client.post(f"/razbor/sales/{s1.id}", data={"action": "pack", "place": "у окошка", "back": "/razbor/shipping"})
    assert rsvc.stage_of(_sale(s1.stock_item_id)) == "ready_pickup" and _sale(s1.stock_item_id).place == "у окошка"
    assert "Покупатель Фара (самовывоз)" in client.get("/razbor/shipping?st=ready_pickup").text
    client.post(f"/razbor/sales/{s1.id}", data={"action": "sold", "pay_method": "cash", "back": "/razbor/shipping"})
    assert _sale(s1.stock_item_id).status == "sold"

    # Почта: служба по умолчанию — Нова Пошта; без ТТН не отправить; наклейка; отправлено; реестр
    s2 = _sale(items["Бампер (почта)"].id)
    client.post(f"/razbor/sales/{s2.id}", data={"action": "pack", "place": "полка отправок 1", "back": "/razbor/shipping"})
    s2 = _sale(s2.stock_item_id)
    assert s2.carrier_id == np.id and rsvc.stage_of(s2) == "ready_ship"
    client.post(f"/razbor/sales/{s2.id}", data={"action": "ship", "ttn": "", "back": "/razbor/shipping"})
    assert _sale(s2.stock_item_id).status == "reserved"          # ТТН обязателен
    lab = client.get(f"/razbor/shipping/labels?ids={s2.id}").text
    assert "Покупатель Бампер (почта)" in lab and "Львов, отд. 5" in lab and "НАЛОЖКА" in lab and "<svg" in lab
    client.post(f"/razbor/sales/{s2.id}", data={"action": "ship", "ttn": "20450000111222", "back": "/razbor/shipping"})
    assert _sale(s2.stock_item_id).status == "sent"
    table = client.get("/razbor/parcels?q=20450000111222").text
    assert "Покупатель Бампер (почта)" in table and "полка отправок 1" in table and "Нова Пошта" in table
    reg = client.get(f"/razbor/parcels/print?d={date.today().isoformat()}").text
    assert "РЕЕСТР ОТПРАВОК" in reg and "20450000111222" in reg

    # Курьер: ТТН не нужен
    s3 = _sale(items["Дверь (курьер)"].id)
    client.post(f"/razbor/sales/{s3.id}", data={"action": "pack", "carrier_id": str(courier.id), "back": "/razbor/shipping"})
    client.post(f"/razbor/sales/{s3.id}", data={"action": "ship", "ttn": "", "back": "/razbor/shipping"})
    assert _sale(s3.stock_item_id).status == "sent" and _sale(s3.stock_item_id).carrier_id == courier.id
    assert client.get("/api/notify").json()["razbor"] == before

    # Менеджер видит экран (продаёт разбор), но пункта меню у него нет; слесарь — нет доступа
    client.post("/logout")
    login(client, "irina", "irina123")
    page = client.get("/razbor/shipping").text
    assert "Выдача и отправка" in page and not re.search(r'<nav class="sidebar">.*href="/razbor/shipping".*</nav>', page, re.S)
    client.post("/logout")
    login(client, "andrey", "andrey123")
    assert client.get("/razbor/shipping").status_code == 403 and client.get("/razbor/parcels").status_code == 403
    client.post("/logout")
