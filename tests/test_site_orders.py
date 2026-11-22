"""22.11.2026 (брат): «Заявки с сайта», «Нет на сайте», время привоза.
- заявка (вручную и с сайта по ключу): «✓ Принять» → деталь в резерве под покупателя, откуда = «сайт»;
  деталь не в наличии — принять нельзя; «Отказать» — с причиной;
- «Нет на сайте»: утверждена и в наличии, галочки нет → плитка, 🟠 цифра у «Разбора», «＋ На сайт»;
- время привоза: заказал с часом → «привезут ДД.ММ к ЧЧ:ММ», опаздывает — когда прошёл этот час."""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, PartCatalog, Purchase, SiteOrder, StockItem, UsedSale
from app.services import stock as ssvc

from .conftest import approve_razbor, login


def _used_parts(client, make):
    login(client, "dmitro", "dmitro123")
    client.post("/razbor/donors", data={"make": make, "model": "Golf", "year": "2016"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == make))
    client.post(f"/razbor/donors/{d.id}/parts/bulk", data={"category": "Оптика", "subcategory": "Фары",
                                                           "lines": f"Фара левая ({make}); 3000\nФара правая ({make}); 3100"})
    approve_razbor()
    with SessionLocal() as db:
        return {c.name: i for i, c in db.execute(select(StockItem, PartCatalog).join(
            PartCatalog, PartCatalog.id == StockItem.catalog_id).where(StockItem.donor_id == d.id))}


def test_nosite_tile_badge_and_button(client):
    items = _used_parts(client, "VW-нетсайт")
    left = items["Фара левая (VW-нетсайт)"]
    with SessionLocal() as db:   # списком заводятся сразу «на сайт» — снимаем галочку (забыли / сняли с сайта)
        for i in items.values():
            db.get(StockItem, i.id).publish_on_site = False
        db.commit()
    page = client.get("/razbor?v=parts&f=nosite").text
    assert "Нет на сайте" in page and "Фара левая (VW-нетсайт)" in page and "＋ На сайт" in page
    b = client.get("/api/notify").json()["badges"].get("/razbor", [])
    assert any(x["c"] == "orange" and x["n"] >= 2 for x in b)
    client.post(f"/razbor/parts/{left.id}/to-site", data={"back": "/razbor?v=parts&f=nosite"})
    with SessionLocal() as db:
        assert db.get(StockItem, left.id).publish_on_site
    assert "Фара левая (VW-нетсайт)" not in client.get("/razbor?v=parts&f=nosite").text


def test_site_order_manual_accept_and_decline(client):
    items = _used_parts(client, "VW-заявка")
    code = items["Фара правая (VW-заявка)"].code
    client.post("/logout")
    login(client, "director", "director123")   # директор тоже видит «Заявки с сайта»
    assert client.get("/site/orders").status_code == 200
    r = client.post("/site/orders", data={"last_name": "Петренко", "first_name": "Иван", "phone": "0671234500",
                                          "delivery": "np", "city": "Киев", "np_branch": "Отделение 12", "pay": "cod",
                                          "code": [code, "Р-99999"], "qty": ["1", "1"], "price": ["", ""]},
                    follow_redirects=True)
    assert "Нельзя" not in r.text
    with SessionLocal() as db:
        o = db.scalar(select(SiteOrder).where(SiteOrder.last_name == "Петренко"))
    b = client.get("/api/notify").json()["badges"].get("/site/orders", [])
    assert any(x["c"] == "red" for x in b)
    # одной детали нет — принять нельзя
    client.post(f"/site/orders/{o.id}/accept")
    with SessionLocal() as db:
        assert db.get(SiteOrder, o.id).status == "new"
    page = client.get("/site/orders?tab=new").text
    assert "нет такого кода" in page
    # убрали несуществующую — принимаем: резерв, откуда = сайт, Нова Пошта и наложка
    from app.models import SiteOrderLine
    with SessionLocal() as db:
        bad = db.scalar(select(SiteOrderLine).where(SiteOrderLine.site_order_id == o.id, SiteOrderLine.code == "Р-99999"))
    client.post(f"/site/orders/{o.id}/lines/{bad.id}/delete")
    client.post(f"/site/orders/{o.id}/accept")
    with SessionLocal() as db:
        assert db.get(SiteOrder, o.id).status == "accepted"
        s = db.scalar(select(UsedSale).where(UsedSale.stock_item_id == items["Фара правая (VW-заявка)"].id))
        assert s.status == "reserved" and s.channel == "site" and s.delivery == "np" and s.cod
        assert "Киев" in s.np_address and s.buyer_name == "Петренко Иван"
    # вторая заявка на ту же деталь — уже в резерве
    client.post("/site/orders", data={"last_name": "Сидоренко", "first_name": "Олег", "phone": "0671234501",
                                      "delivery": "pickup", "pay": "on_pickup", "code": [code], "qty": ["1"], "price": [""]})
    with SessionLocal() as db:
        o2 = db.scalar(select(SiteOrder).where(SiteOrder.last_name == "Сидоренко"))
    assert "нет в наличии" in client.get("/site/orders?tab=new").text
    client.post(f"/site/orders/{o2.id}/decline", data={"reason": "уже продана"})
    with SessionLocal() as db:
        assert db.get(SiteOrder, o2.id).status == "declined"


def test_site_order_api_key(client):
    _used_parts(client, "VW-апи")
    client.post("/logout")
    assert client.post("/api/site/orders", json={"items": []}, headers={"X-Site-Key": "x" * 30}).status_code == 401
    login(client, "admin", "admin123")
    client.post("/site/orders/api-key")
    from app.services import settings as st
    with SessionLocal() as db:
        from app.models import Branch
        key = st.get(db, db.scalar(select(Branch.id)), "site_api")["key"]
    client.post("/logout")
    r = client.post("/api/site/orders", headers={"X-Site-Key": key}, json={
        "last_name": "Онлайн", "first_name": "Покупатель", "phone": "0679990011", "delivery": "courier",
        "address": "ул. Тестовая, 1", "pay": "card", "items": [{"code": "Р-0001", "qty": 1}]})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["number"].startswith("З-")
    with SessionLocal() as db:
        assert db.scalar(select(SiteOrder).where(SiteOrder.last_name == "Онлайн")).source == "site"


def test_expected_time_and_late():
    p = Purchase(status="ordered")
    ssvc.set_expected(p, date.today(), "14:00")
    assert p.expected_time == "14:00" and ssvc.expected_label(p).endswith("к 14:00")
    today = datetime.combine(date.today(), datetime.min.time())
    assert not ssvc.is_late(p, today + timedelta(hours=13, minutes=59))
    assert ssvc.is_late(p, today + timedelta(hours=14, minutes=1))
    ssvc.set_expected(p, date.today(), "25:99")          # не из списка — время не меняем
    assert p.expected_time == "14:00"
    ssvc.set_expected(p, date.today() + timedelta(days=1))   # новая дата без времени — время сброшено
    assert p.expected_time == "" and not ssvc.is_late(p)
