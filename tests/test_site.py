"""«На сайт» (22.11.2026, брат): новые запчасти на продажу и б/у разбора.
- запчастист «Продать» в «Лишнем после отказов» → деталь в «На сайт → Новые» по цене для клиента, ждёт утверждения;
- кладовщик разбора цену не меняет; директор утверждает → «На сайте»; продажа — как у б/у (UsedSale, site_new_id);
- деталь можно взять в наряд: свободного нет — «Нет на складе»; «На продажу» из карточки склада;
- выгрузка для сайта — новые и б/у вместе (колонка «Тип»)."""
import io
import uuid
import zipfile
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PartLeftover, StockItem, UsedSale, WoPart, WorkOrder
from app.models.razbor import SiteNew

from .conftest import login
from .test_refuse_parts import _new_order, _stock_item


def _leftover_for_sale(client, name):
    """Деталь на складе + отказ клиента по наряду (лишнее на складе, решить)."""
    cat = _stock_item(client, name, qty="2")
    url = _new_order(client, "КА 8801 СТ", "0508801000")
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        current_branch_id.set(wo.branch_id)
        p = WoPart(work_order_id=wid, name=name, catalog_id=cat.id, qty=Decimal(1), price=Decimal("650"),
                   cost=Decimal("400"), status="archived", branch_id=wo.branch_id)
        db.add(p)
        db.flush()
        lo = PartLeftover(work_order_id=wid, wo_part_id=p.id, catalog_id=cat.id, qty=Decimal(1), cost=Decimal("400"),
                          reason="купил сам", status="stock", branch_id=wo.branch_id)
        db.add(lo)
        db.commit()
        return cat, lo.id, wid


def test_refused_part_to_site_approve_and_sell(client):
    cat, lo_id, wid = _leftover_for_sale(client, "Рычаг тест-сайт")
    login(client, "oleg", "oleg123")
    client.post(f"/stock/leftovers/{lo_id}", data={"action": "sale"})
    client.post("/logout")
    with SessionLocal() as db:
        sn = db.scalar(select(SiteNew).where(SiteNew.leftover_id == lo_id))
        assert sn and sn.price == Decimal("650") and sn.state == "pending" and sn.code.startswith("Н-")
        sid = sn.id
    # кладовщик разбора видит, но продать до утверждения нельзя и цену не меняет
    login(client, "dmitro", "dmitro123")
    page = client.get("/site?t=new&f=wait").text
    assert "Рычаг тест-сайт" in page and "650,00" in page and "Утвердить" not in page
    client.post(f"/site/new/{sid}/sell", data={"qty": "1", "buyer_name": "Тест", "action": "sold"})
    with SessionLocal() as db:
        assert not db.scalar(select(UsedSale.id).where(UsedSale.site_new_id == sid))
    client.post("/logout")
    # директор: красная цифра у «На сайт», утверждает
    login(client, "director", "director123")
    badges = client.get("/api/notify").json()["badges"].get("/site", [])
    assert any(b["c"] == "red" and b["n"] >= 1 for b in badges)
    client.post(f"/site/new/{sid}/approve")
    client.post("/logout")
    login(client, "dmitro", "dmitro123")
    # 26.11.2026: утвердили → «Выложить на сайт»; без 2 фото и описания «Выложил» не проходит, в выгрузке нет
    assert "Рычаг тест-сайт" in client.get("/site?t=new&f=post").text
    assert "Рычаг тест-сайт" not in client.get("/site?t=new&f=site").text
    client.post(f"/site/new/{sid}/post")
    with SessionLocal() as db:
        assert db.get(SiteNew, sid).posted_at is None
    JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
    client.post(f"/site/new/{sid}", data={"description": "Новый, в упаковке"},
                files=[("photos", ("a.jpg", JPEG, "image/jpeg")), ("photos", ("b.jpg", JPEG, "image/jpeg"))])
    client.post(f"/site/new/{sid}/post")
    assert "Рычаг тест-сайт" in client.get("/site?t=new&f=site").text
    # в выгрузке для сайта — новая деталь с типом «Новая»
    z = zipfile.ZipFile(io.BytesIO(client.get("/razbor/export.zip").content))
    js = z.read("razbor.json").decode()
    assert "Рычаг тест-сайт" in js and '"type": "new"' in js
    # продажа в резерв → обычная продажа разбора (Выдача и отправка), со склада ушла 1 шт
    r = client.post(f"/site/new/{sid}/sell", data={"qty": "1", "buyer_name": "Покупатель сайта", "buyer_phone": "0671110000",
                                                   "delivery": "pickup", "action": "reserve"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        s = db.scalar(select(UsedSale).where(UsedSale.site_new_id == sid))
        assert s and s.status == "reserved" and s.price == Decimal("650")
    assert "Покупатель сайта" in client.get("/razbor/shipping?st=prepare").text
    # всё выставленное (1 шт) уже в резерве — на сайте больше нет
    assert "Рычаг тест-сайт" not in client.get("/site?t=new&f=site").text
    client.post("/logout")


def test_taken_into_order_leaves_site_and_offer_from_stock(client):
    cat, lo_id, wid = _leftover_for_sale(client, "Опора тест-сайт")
    login(client, "oleg", "oleg123")
    client.post(f"/stock/leftovers/{lo_id}", data={"action": "sale"})
    client.post("/logout")
    with SessionLocal() as db:
        sn = db.scalar(select(SiteNew).where(SiteNew.leftover_id == lo_id))
        sn.state = "approved"
        # другой наряд забрал обе штуки в резерв — свободного нет
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id)).all()
        from app.services import stock as ssvc
        current_branch_id.set(items[0].branch_id)
        ssvc.move(db, "reserve", items[0], Decimal(2), comment="тест: в наряд")
        db.commit()
    login(client, "dmitro", "dmitro123")
    page = client.get("/site?t=new&f=nostock").text
    assert "Опора тест-сайт" in page
    assert "Опора тест-сайт" not in client.get("/site?t=new&f=site").text
    client.post("/logout")
    # «На продажу» из карточки склада: цена — закупка + наценка, ждёт утверждения
    cat2 = _stock_item(client, "Фильтр тест-сайт", qty="3")
    with SessionLocal() as db:
        item = db.scalar(select(StockItem).where(StockItem.catalog_id == cat2.id))
    login(client, "pavel", "pavel123")
    assert "На продажу" in client.get(f"/stock/items/{item.id}").text
    client.post(f"/stock/items/{item.id}/to-site", data={"qty": "2"})
    client.post("/logout")
    with SessionLocal() as db:
        sn2 = db.scalar(select(SiteNew).where(SiteNew.catalog_id == cat2.id))
        assert sn2 and sn2.source == "manual" and sn2.qty == 2 and sn2.price > Decimal("400") and sn2.state == "pending"
    # мастер раздела не видит
    login(client, "andrey", "andrey123")
    assert client.get("/site", follow_redirects=False).status_code in (303, 403)
    client.post("/logout")
