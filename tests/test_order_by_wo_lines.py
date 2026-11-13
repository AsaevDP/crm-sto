"""Заказ по наряду по одной детали: все сочетания поставщиков (13.11.2026; прогон ловил «Нет доступа» после «Заказал»)."""
import itertools

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Purchase, PurchaseLine

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo


def test_line_by_line_all_supplier_combos(client):
    """Поставщик и «Заказал» по каждой детали — все сочетания двух поставщиков на три детали; черновиков не остаётся."""
    for i, combo in enumerate(itertools.product("XY", repeat=3)):
        _as(client, "irina")
        url = _create(client, scenario="A", photos=1, plate=f"KA3{i:03d}AA", phone=f"0670003{i:03d}", works="Т").json()["url"]
        _as(client, "oleg")
        for n in range(3):
            client.post(f"{url}/parts", data={"name": f"Д{i}-{n}", "qty": "1", "cost": "100", "markup_pct": "30", "action": "order"})
        agree(client, url, then="oleg")
        wo = _wo(url)

        def lines():
            with SessionLocal() as db:
                ps = [p.id for p in db.scalars(select(Purchase).where(Purchase.work_order_id == wo.id, Purchase.status == "draft"))]
                return db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id.in_(ps))).all() if ps else []
        for l, s in zip(lines(), combo):
            r = client.post(f"/purchase-lines/{l.id}/supplier", data={"supplier_new": s})
            assert r.status_code == 200, (combo, "supplier", r.status_code)
        for l in lines():
            r = client.post(f"/purchase-lines/{l.id}/ordered", data={})   # без «back» — экран после нажатия открывается
            assert r.status_code == 200, (combo, "ordered", r.status_code)
        assert not lines()
