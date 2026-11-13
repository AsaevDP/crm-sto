"""«Заказ по наряду» (13.11.2026, брат): у каждой детали свой поставщик, «Один поставщик на весь наряд»,
«Заказал» по детали — сразу у кладовщика; «Приход» — по поставщикам, сразу по нескольким нарядам."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import PartCatalog, Purchase, PurchaseLine, Supplier, WoPart

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo


def _line(name):
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.name == name))
        return db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == part.id))


def _p(line):
    with SessionLocal() as db:
        return db.get(Purchase, db.get(PurchaseLine, line.id).purchase_id)


def _sup(name):
    with SessionLocal() as db:
        return db.scalar(select(Supplier.id).where(Supplier.name == name))


def test_short_delivery_one_part_missing(client):
    """14.11.2026 (брат): прокладку привезли, болты нет (вписали 0) → «Неполная поставка» и прокладка в «Принятых».
    Раньше при 0 накладная оставалась «привезли — ждёт приёмки» и не была видна ни там, ни там."""
    import uuid
    _as(client, "oleg")
    r = client.post("/purchases/new", data={"supplier_new": "Недовоз ЗН"})
    pid = uuid.UUID(r.url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid}/lines", data={"name": "Прокладка ЗН", "qty": "1", "price": "1850"})
    client.post(f"/purchases/{pid}/lines", data={"name": "Болты ЗН", "qty": "1", "price": "980"})
    client.post(f"/purchases/{pid}/ordered", data={})
    with SessionLocal() as db:
        ls = {db.get(PartCatalog, l.catalog_id).name: l.id
              for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == pid))}
    _as(client, "pavel")
    for attempt in range(2):   # второй раз — после «Довезли остальное», а болтов снова нет
        client.post(f"/purchases/{pid}/delivered", data={})
        r = client.post(f"/purchases/{pid}/receive", data={"all": str(pid), "short": "1",
                                                             f"qty_{ls['Прокладка ЗН']}": "1", f"qty_{ls['Болты ЗН']}": "0"})
        assert "Неполная поставка — ждём: Болты ЗН" in r.text
        with SessionLocal() as db:
            assert db.get(Purchase, pid).status == "partial"
        for tab in ("partial", "arrived"):
            page = client.get(f"/purchases?tab={tab}").text
            assert f"/purchases/{pid}" in page, (attempt, tab)
        assert "✓ принято:</span> Прокладка ЗН" in client.get("/purchases?tab=arrived").text
        if attempt == 0:
            assert "Довезли остальное" in client.get("/purchases?tab=partial").text
    # Ничего не привезли вовсе — накладная снова «Ждём»
    _as(client, "oleg")
    pid2 = uuid.UUID(client.post("/purchases/new", data={"supplier_new": "Недовоз ЗН"}).url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid2}/lines", data={"name": "Сальник ЗН", "qty": "1", "price": "100"})
    client.post(f"/purchases/{pid2}/ordered", data={})
    with SessionLocal() as db:
        l2 = db.scalar(select(PurchaseLine.id).where(PurchaseLine.purchase_id == pid2))
    _as(client, "pavel")
    client.post(f"/purchases/{pid2}/delivered", data={})
    client.post(f"/purchases/{pid2}/receive", data={"all": str(pid2), "short": "1", f"qty_{l2}": "0"})
    with SessionLocal() as db:
        assert db.get(Purchase, pid2).status == "ordered"


def test_order_by_wo(client):
    _as(client, "irina")
    url_a = _create(client, scenario="A", photos=1, plate="KA2001AA", phone="0670002001", works="Тормоза").json()["url"]
    url_b = _create(client, scenario="A", photos=1, plate="KA2002AA", phone="0670002002", works="Подвеска").json()["url"]
    _as(client, "oleg")
    for n in ("Колодки ЗН", "Диск ЗН", "Шланг ЗН"):
        client.post(f"{url_a}/parts", data={"name": n, "qty": "1", "cost": "100", "markup_pct": "30", "action": "order"})
    client.post(f"{url_b}/parts", data={"name": "Стойка ЗН", "qty": "1", "cost": "500", "markup_pct": "30", "action": "order"})
    agree(client, url_a, then=None)
    agree(client, url_b, then="oleg")
    wa, wb = _wo(url_a), _wo(url_b)

    # Карточка в «Моих заказах» ведёт в «Заказ по наряду», а не в наряд
    mine = client.get("/my-orders").text
    assert f'href="/my-orders/wo/{wa.id}"' in mine and "Открыть заказ" in mine
    page = client.get(f"/my-orders/wo/{wa.id}").text
    assert "Один поставщик на весь наряд" in page and "Колодки ЗН" in page and "Шланг ЗН" in page

    # Без поставщика — «Заказал всё» ничего не заказывает
    client.post(f"/my-orders/wo/{wa.id}/ordered", data={})
    assert _p(_line("Колодки ЗН")).status == "draft"
    assert not _line("Колодки ЗН").ordered_at

    # «Один поставщик на весь наряд» → у всех; потом у шланга — другой: в наряде два поставщика
    client.post(f"/my-orders/wo/{wa.id}/supplier", data={"supplier_new": "Элит ЗН"})
    elit = _sup("Элит ЗН")
    assert {_p(_line(n)).supplier_id for n in ("Колодки ЗН", "Диск ЗН", "Шланг ЗН")} == {elit}
    client.post(f"/purchase-lines/{_line('Шланг ЗН').id}/supplier", data={"supplier_new": "Омега ЗН"})
    omega = _sup("Омега ЗН")
    assert _p(_line("Шланг ЗН")).supplier_id == omega and _p(_line("Колодки ЗН")).supplier_id == elit
    assert _p(_line("Шланг ЗН")).work_order_id == wa.id and _p(_line("Шланг ЗН")).status == "draft"

    # «Заказал» по одной детали — сразу в накладной (кладовщик видит), остальные ещё в черновике
    l1 = _line("Колодки ЗН")
    client.post(f"/purchase-lines/{l1.id}/ordered", data={"expected_on": "2030-01-05", "back": f"/my-orders/wo/{wa.id}"})
    p1 = _p(l1)
    assert p1.status == "ordered" and p1.supplier_id == elit and str(p1.expected_on) == "2030-01-05"
    assert _p(_line("Диск ЗН")).status == "draft"
    _as(client, "pavel")
    rec = client.get("/stock/receive").text
    assert f'name="qty_{l1.id}"' in rec and f'name="qty_{_line("Диск ЗН").id}"' not in rec

    # «Заказал всё»: диск — в ту же накладную Элит (её ещё не привезли), шланг — в накладную Омеги
    _as(client, "oleg")
    client.post(f"/my-orders/wo/{wa.id}/ordered", data={})
    assert _p(_line("Диск ЗН")).id == p1.id
    assert _p(_line("Шланг ЗН")).status == "ordered" and _p(_line("Шланг ЗН")).id != p1.id
    page = client.get(f"/my-orders/wo/{wa.id}").text
    assert "Заказано · 3" in page and "Элит ЗН" in page and "Омега ЗН" in page

    # Второй наряд — тоже у Элит, «Заказал» прямо из списка в «Моих заказах»
    lb = _line("Стойка ЗН")
    client.post(f"/purchase-lines/{lb.id}/supplier", data={"supplier_id": str(elit), "back": "/my-orders"})
    assert f'action="/purchase-lines/{lb.id}/ordered"' in client.get("/my-orders").text
    client.post(f"/purchase-lines/{lb.id}/ordered", data={"back": "/my-orders"})
    assert _p(lb).status == "ordered" and _p(lb).work_order_id == wb.id   # своя накладная на свой наряд

    # Кладовщик: Элит — одна группа на оба наряда; «Принять всё» у Элит — Омегу не трогает
    _as(client, "pavel")
    rec = client.get("/stock/receive").text
    assert rec.count('name="sup" value="элит зн"') == 1 and wa.number in rec and wb.number in rec
    client.post("/stock/receive", data={"sup": "элит зн"})
    assert _line("Колодки ЗН").received and _line("Диск ЗН").received and _line("Стойка ЗН").received
    assert not _line("Шланг ЗН").received
    with SessionLocal() as db:
        assert db.get(WoPart, _line("Стойка ЗН").wo_part_id).status == "reserved"

    # 14.11.2026 (брат): неполная поставка — принятая деталь сразу в «Принятых», не ждёт остальных.
    # Даже если потом нажали «Довезли остальное» (накладная снова «привезли — ждёт приёмки»).
    po = _p(_line("Шланг ЗН"))
    _as(client, "oleg")
    client.post(f"/purchases/{po.id}/lines", data={"name": "Хомут ЗН", "qty": "1", "price": "50"})
    _as(client, "pavel")
    client.post(f"/purchases/{po.id}/delivered", data={})
    client.post("/stock/receive", data={"line_id": str(_line("Шланг ЗН").id)})
    assert _p(_line("Шланг ЗН")).status == "partial"
    page = client.get("/purchases?tab=arrived").text
    assert f"/purchases/{po.id}" in page and "✓ принято:" in page and "ждём: Хомут ЗН" in page
    assert "Довезли остальное" in page
    client.post(f"/purchases/{po.id}/delivered", data={})          # «Довезли остальное»
    page = client.get("/purchases?tab=arrived").text
    assert f"/purchases/{po.id}" in page and "довезли, ждёт приёмки: Хомут ЗН" in page
