"""20.11.2026 (брат): склад — две зоны со своими окошками.

- В наряде новые и б/у: «Запросить выдачу» → ДВА запроса: новые — кладовщику (Павел), б/у — кладовщику разбора (Дмитрий).
- Кладовщик выдаёт только свою зону; соседнюю видит «для сведения», без кнопок.
- У кладовщика разбора — «Запросы на выдачу» в меню, 🔴 цифра, «Готово к выдаче» / «Выдано».
- Менеджер видит, у какого окошка забирать (склад / разбор).
- Главный кладовщик выдаёт в обеих зонах; кладовщик зоны на работе (по табелю) — его цифры «для сведения».
- Кладовщик разбора: инвентаризация детали разбора — можно, детали нового склада — нельзя; ячейки заводит.
"""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import IssueRequest, PartCatalog, StockItem, User, WoPart, WorkOrder
from app.services import stock as ssvc

from .conftest import login
from .test_step3 import _create
from .test_warranty import _presence


def _item(name, used: bool, cost="200"):
    with SessionLocal() as db:
        br = db.scalar(select(User).where(User.login == "pavel")).branch_id
        current_branch_id.set(br)
        cat = ssvc.get_or_create_catalog(db, name, "", "")
        if used:
            cat.kind = "used"
        item = ssvc.get_or_create_item(db, cat.id, None)
        if used:
            item.code = "Р-T" + str(abs(hash(name)) % 100000)   # у детали разбора есть код (как Р-0001)
        item.cost, item.sale_price = Decimal(cost), Decimal(cost)
        ssvc.move(db, "in", item, 2, comment="тест")
        db.commit()
        return cat.id, item.id


def _order_with_new_and_used(client, plate, phone):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone).json()["url"]
    client.post("/logout")
    new_cat, _ = _item(f"Втулка (зоны {plate})", used=False)
    used_cat, used_item = _item(f"Молдинг б/у (зоны {plate})", used=True)
    with SessionLocal() as db:
        wo = db.scalar(select(WorkOrder).where(WorkOrder.id == __import__("uuid").UUID(url.rsplit("/", 1)[1])))
        current_branch_id.set(wo.branch_id)
        wo.mechanic_id = db.scalar(select(User.id).where(User.login == "andrey"))
        wo.status = "instock"
        for cat_id in (new_cat, used_cat):
            c = db.get(PartCatalog, cat_id)
            p = WoPart(work_order_id=wo.id, catalog_id=c.id, name=c.name, qty=1, cost=200, price=300)
            db.add(p)
            db.flush()
            assert ssvc.reserve_from_stock(db, wo.branch_id, p)
        db.commit()
        return url, wo.id, used_item


def _badge(client, color, word):
    return sum(b["n"] for b in client.get("/api/notify").json()["badges"].get("/stock/issues", [])
               if b["c"] == color and word in b["t"] and not b.get("info"))


def test_two_zones_two_windows(client):
    url, wo_id, used_item = _order_with_new_and_used(client, "КЗ 2001 КЗ", "0502001001")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/issue-request", data={})
    client.post("/logout")
    with SessionLocal() as db:
        reqs = {r.zone: r for r in db.scalars(select(IssueRequest).where(IssueRequest.work_order_id == wo_id))}
    assert set(reqs) == {"new", "razbor"}
    rn, rr = reqs["new"].id, reqs["razbor"].id

    # Павел: новые — с кнопками; б/у — для сведения, выдать нельзя
    login(client, "pavel", "pavel123")
    assert f"/stock/issues/{rn}/ready" in client.get("/stock/issues").text
    rz = client.get("/stock/issues?tab=razbor").text
    assert "Для сведения" in rz and f"/stock/issues/{rr}/ready" not in rz and "Молдинг б/у (зоны КЗ 2001 КЗ)" in rz
    assert client.post(f"/stock/issues/{rr}/ready", follow_redirects=False).status_code == 403
    assert client.post(f"/stock/issues/{rr}/issue", follow_redirects=False).status_code == 403
    client.post("/logout")

    # Дмитрий: «Запросы на выдачу» в меню, красная цифра, кнопки на б/у; новые — для сведения
    login(client, "dmitro", "dmitro123")
    assert 'href="/stock/issues"' in client.get("/razbor").text.split("<main", 1)[0]
    assert _badge(client, "red", "б/у") >= 1
    page = client.get("/stock/issues?tab=razbor").text
    assert f"/stock/issues/{rr}/ready" in page and "Слесарь" in page
    assert f"/stock/issues/{rn}/ready" not in client.get("/stock/issues?tab=parts").text
    client.post(f"/stock/issues/{rr}/ready")
    with SessionLocal() as db:
        assert db.get(IssueRequest, rr).status == "ready"
    client.post("/logout")

    # Менеджер: видно, что б/у забирать у окошка разбора; «Выдано» за кладовщика разбора
    login(client, "irina", "irina123")
    assert "б/у у окошка разбора" in client.get("/ready").text
    client.post(f"/stock/issues/{rr}/issue", data={"back": "/ready"})
    with SessionLocal() as db:
        assert db.get(IssueRequest, rr).status == "issued" and db.get(IssueRequest, rn).status == "new"
    client.post("/logout")
    login(client, "pavel", "pavel123")   # база в тестах общая — закрыть
    client.post(f"/stock/issues/{rn}/issue")
    client.post("/logout")


def test_headkeeper_both_zones_and_info_by_timesheet(client):
    url, wo_id, _ = _order_with_new_and_used(client, "КЗ 2002 КЗ", "0502002002")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/issue-request", data={})
    client.post("/logout")
    with SessionLocal() as db:
        reqs = {r.zone: r.id for r in db.scalars(select(IssueRequest).where(IssueRequest.work_order_id == wo_id))}
    _presence("pavel", "in")       # кладовщик склада на работе — у главного его зона «для сведения»
    _presence("dmitro", "out")     # кладовщика разбора нет — главный подменяет: обычная красная цифра
    login(client, "glav", "glav123")
    b = client.get("/api/notify").json()["badges"].get("/stock/issues", [])
    assert any(x["c"] == "red" and "запчасти" in x["t"] and x.get("info") for x in b)
    assert any(x["c"] == "red" and "б/у" in x["t"] and not x.get("info") for x in b)
    assert f"/stock/issues/{reqs['new']}/ready" in client.get("/stock/issues").text
    assert f"/stock/issues/{reqs['razbor']}/ready" in client.get("/stock/issues?tab=razbor").text
    client.post(f"/stock/issues/{reqs['razbor']}/issue")
    client.post(f"/stock/issues/{reqs['new']}/issue")
    with SessionLocal() as db:
        assert all(db.get(IssueRequest, r).status == "issued" for r in reqs.values())
    client.post("/logout")
    _presence("pavel", "out")


def test_razbor_keeper_inventory_only_razbor(client):
    _, used_item = _item("Кронштейн б/у (инвентаризация)", used=True)
    _, new_item = _item("Хомут (инвентаризация)", used=False)
    login(client, "dmitro", "dmitro123")
    page = client.get(f"/razbor/parts/{used_item}").text
    assert "Инвентаризация" in page
    client.post(f"/stock/items/{used_item}/adjust", data={"actual": "1", "reason": "одно разбилось"})
    with SessionLocal() as db:
        assert ssvc.balances(db, db.get(StockItem, used_item).branch_id, [used_item])[used_item]["on_hand"] == 1
    assert client.post(f"/stock/items/{new_item}/adjust", data={"actual": "0", "reason": "x"},
                       follow_redirects=False).status_code == 403
    client.post("/stock/locations", data={"rack": "РЗ", "shelf": "1", "place": "1"})
    assert "РЗ / 1 / 1" in client.get("/stock/locations").text
    client.post("/logout")
