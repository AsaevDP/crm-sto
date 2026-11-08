"""Шаг 7: разбор. Критерий брифа: выгрузка открывается в Excel, фото на месте."""
import io
import json
import zipfile
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, StockItem, UsedSale
from app.services import razbor as rsvc

from .conftest import login
from .test_step3 import JPEG

FETCH = {"x-requested-with": "fetch"}


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _item(code):
    with SessionLocal() as db:
        return db.scalar(select(StockItem).where(StockItem.code == code))


def test_donor_parts_sale_and_export(client):
    _as(client, "dmitro")  # кладовщик-контентщик
    client.post("/razbor/donors", data={"make": "BMW", "model": "X5 E70", "year": "2009", "vin": "wbafe41000lx12345",
                                        "purchase_price": "45000"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.vin == "WBAFE41000LX12345"))
    assert d.code.startswith("Д-")
    client.post(f"/razbor/donors/{d.id}", data={"make": "BMW", "model": "X5 E70", "year": "2009", "engine": "3.0 дизель M57",
                                                "gearbox": "АКПП", "purchase_price": "45000", "expenses": "5000", "status": "dismantling"})
    files = [("photos", ("a.jpg", JPEG, "image/jpeg")), ("photos", ("b.jpg", JPEG, "image/jpeg"))]
    client.post(f"/razbor/donors/{d.id}/parts", data={"name": "Фара передняя левая (тест)", "price": "6500", "qty": "1",
                                                      "condition": "good", "publish": "1", "description": "Без трещин"}, files=files)
    client.post(f"/razbor/donors/{d.id}/parts", data={"name": "Зеркало правое (тест)", "price": "2200", "qty": "1",
                                                      "condition": "excellent", "publish": "1"})
    with SessionLocal() as db:
        items = db.scalars(select(StockItem).where(StockItem.donor_id == d.id).order_by(StockItem.code)).all()
        codes = [i.code for i in items]
    assert all(c.startswith("Р-") for c in codes) and len(codes) == 2
    lamp, mirror = codes

    # Экспорт: обе детали, Excel открывается, фото в архиве
    r = client.get("/razbor/export.zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert {"razbor.xlsx", "razbor.csv", "razbor.json"} <= set(names)
    assert f"photos/{lamp}_1.jpg" in names and f"photos/{lamp}_2.jpg" in names
    ws = load_workbook(io.BytesIO(z.read("razbor.xlsx"))).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:2] == ("Код", "Название")
    assert {row[0] for row in rows[1:]} >= {lamp, mirror}
    lamp_row = next(row for row in rows if row[0] == lamp)
    assert lamp_row[1] == "Фара передняя левая (тест)" and lamp_row[9] == 6500 and f"{lamp}_1.jpg" in lamp_row[13]
    assert z.read("razbor.csv")[:3] == b"\xef\xbb\xbf"  # BOM — Excel понимает кириллицу
    assert json.loads(z.read("razbor.json"))["items"]

    # Резерв под покупателя → в экспорт больше не идёт; «Продано» с ТТН → остаток 0
    _as(client, "irina")
    client.post(f"/razbor/parts/{_item(lamp).id}/sell", data={"action": "reserve", "buyer_name": "Иван", "buyer_phone": "0671112233",
                                                              "price": "6300", "qty": "1", "delivery": "np"})
    with SessionLocal() as db:
        s = db.scalar(select(UsedSale).where(UsedSale.stock_item_id == _item(lamp).id))
        b = rsvc.ssvc.balances(db, s.branch_id, [s.stock_item_id])[s.stock_item_id]
    assert s.status == "reserved" and b["free"] == 0
    with SessionLocal() as db:
        assert lamp not in [x["Код"] for x in rsvc.export_rows(db, s.branch_id)]
    client.post(f"/razbor/sales/{s.id}", data={"action": "sold", "ttn": "20450000000000", "pay_method": "card"})
    with SessionLocal() as db:
        s = db.get(UsedSale, s.id)
        b = rsvc.ssvc.balances(db, s.branch_id, [s.stock_item_id])[s.stock_item_id]
        e = rsvc.economy(db, s.branch_id, db.get(Donor, d.id))
    assert s.status == "sold" and b["on_hand"] == 0 and s.ttn == "20450000000000"
    assert (e.invested, e.sold, e.on_shelf) == (Decimal("50000.00"), Decimal("6300.00"), Decimal("2200.00"))
    assert "Фара передняя левая (тест)" in client.get("/money").text  # продажа видна в кассе за день

    # Запчастист видит б/у как альтернативу в подборе
    _as(client, "oleg")
    found = client.get("/api/catalog/search?q=Зеркало", headers=FETCH).json()["items"]
    assert found and found[0]["used"] and found[0]["code"] == mirror and found[0]["cost"] == 2200


def test_mechanic_has_no_razbor(client):
    _as(client, "andrey")
    assert client.get("/razbor").status_code == 403
