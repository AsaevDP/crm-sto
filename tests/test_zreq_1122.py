"""Правки 22.11.2026 (брат): машина с улицы на мойку — перед серыми; варианты запчастей у запчастиста в новом наряде
(«ещё вариант» любой кнопкой, то же название — в ту же группу); «Выбрать со склада»; ✅ у «К работе»; «Изменить»
у проблемы."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import WoFinding, WoPart, WorkOrder, WoWork
from app.services import wash as wsvc

from .conftest import login
from .test_step3 import _create
from .test_wash_queue import _repair_car
from .test_wash import _intake, _wo_by_plate


def _order_url(r):
    return r.json()["url"]


def test_quick_wash_goes_before_grey(client):
    grey = _repair_car(client, "АА 9101 ММ", "0671119101", status="approve")   # мыть пока нельзя
    login(client, "irina", "irina123")
    # менеджер уже расставлял очередь — у всех машин есть место
    client.post(f"/wash/{grey.id}/move", data={"where": "last"})
    assert _intake(client, "АА 9102 ММ", "0671119102", ["Экспресс-мойка (поверхностная)"]).json()["ok"]
    new = _wo_by_plate("АА 9102 ММ")
    with SessionLocal() as db:
        q = wsvc.board(db, new.branch_id)["queue"]
        ids = [c.o.id for c in q]
        first_grey = next(i for i, c in enumerate(q) if c.blocked)
        assert ids.index(new.id) < first_grey   # с улицы — перед серыми
        assert all(not c.blocked for c in q[:ids.index(new.id)])
    # убрать машины теста из очереди, чтобы не мешать другим тестам
    current_branch_id.set(new.branch_id)
    with SessionLocal() as db:
        for o in (db.get(WorkOrder, new.id), db.get(WorkOrder, grey.id)):
            o.status = "cancelled"
        db.commit()
    client.post("/logout")


def test_partsman_options_in_new_order(client):
    login(client, "irina", "irina123")
    url = _order_url(_create(client, plate="ВВ 2211 ВВ", phone="0672211221"))
    client.post("/logout")
    login(client, "oleg", "oleg123")
    page = client.get(url).text
    assert "Выбрать со склада" in page and "Замена передних колодок" in page
    # первый вариант — «новая деталь»
    r = client.post(f"{url}/parts", data={"name": "Колодки передние", "brand": "Bosch", "cost": "1000", "qty": "1",
                                          "action": "option", "option_group": ""}, follow_redirects=False)
    assert r.status_code == 303
    # второй — снова «новая деталь», то же название (другой регистр и пробелы) → в ту же группу
    client.post(f"{url}/parts", data={"name": "колодки  передние", "brand": "TRW", "cost": "800", "qty": "1",
                                      "action": "option", "option_group": ""})
    # третий — выбрали группу, но нажали синюю «Со склада или заказать» — всё равно вариант
    oid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        grp = db.scalar(select(WoPart.option_group).where(WoPart.work_order_id == oid, WoPart.option_group.is_not(None)))
    client.post(f"{url}/parts", data={"name": "Колодки передние", "brand": "Ferodo", "cost": "900", "qty": "1",
                                      "action": "auto", "option_group": grp})
    with SessionLocal() as db:
        parts = db.scalars(select(WoPart).where(WoPart.work_order_id == oid)).all()
        assert len(parts) == 3 and {p.option_group for p in parts} == {grp} and all(p.status == "option" for p in parts)
        work = db.scalar(select(WoWork).where(WoWork.work_order_id == oid, WoWork.name == "Замена передних колодок"))
    # ✅ у «К работе»: две детали к работе — две галочки
    for n in ("Датчик износа", "Скоба колодок"):
        client.post(f"{url}/parts", data={"name": n, "cost": "100", "qty": "1", "action": "order",
                                          "wo_work_id": str(work.id)})
    assert "Замена передних колодок  ✅✅<" in client.get(url).text
    # окно «Выбрать со склада»: только свободное на складе, у каждой — раздел и категория
    items = client.get("/api/stock/pick").json()["items"]
    assert all(x["free"] > 0 and x["section"] and x["category"] for x in items)
    client.post("/logout")
    # менеджеру адрес закрыт
    login(client, "irina", "irina123")
    assert client.get("/api/stock/pick").status_code in (403, 303)
    client.post("/logout")


def test_finding_edit(client):
    login(client, "irina", "irina123")
    url = _order_url(_create(client, scenario="B", plate="ВВ 2212 ВВ", phone="0672211222"))
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Течь масла", "urgency": "soon"}, headers={"x-requested-with": "fetch"})
    oid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        f = db.scalar(select(WoFinding).where(WoFinding.work_order_id == oid))
    page = client.get(url).text
    assert "Изменить" in page and "Удалить" in page   # обе кнопки
    client.post(f"{url}/findings/{f.id}/edit", data={"text": "Течь масла с клапанной крышки", "urgency": "urgent"})
    with SessionLocal() as db:
        f2 = db.get(WoFinding, f.id)
        assert f2.text == "Течь масла с клапанной крышки" and f2.urgency == "urgent"
    client.post(f"{url}/diag-done")
    client.post("/logout")
    # менеджер: «Да» → работа; правка проблемы меняет и название работы
    login(client, "irina", "irina123")
    client.post(f"{url}/findings/{f.id}/approval", data={"value": "yes"})
    client.post(f"{url}/findings/{f.id}/edit", data={"text": "Заменить прокладку клапанной крышки", "urgency": "soon"})
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.finding_id == f.id))
        assert w.name == "Заменить прокладку клапанной крышки"
        # после согласования с клиентом — нельзя
        current_branch_id.set(db.get(WorkOrder, oid).branch_id)
        db.get(WorkOrder, oid).status = "work"
        db.commit()
    client.post(f"{url}/findings/{f.id}/edit", data={"text": "Другое", "urgency": "soon"})
    with SessionLocal() as db:
        assert db.get(WoFinding, f.id).text == "Заменить прокладку клапанной крышки"
    client.post("/logout")


def test_urgent_part_badges(client):
    """«🔥 Срочно»: запчастисту 🔴 пока деталь не пришла; менеджеру 🔴 только пока нет даты «привезут»."""
    from datetime import date
    from app.models import Purchase, PurchaseLine
    login(client, "irina", "irina123")
    url = _order_url(_create(client, plate="ВВ 2213 ВВ", phone="0672211223"))
    oid = uuid.UUID(url.rsplit("/", 1)[1])
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Рычаг передний (тест срочно)", "cost": "1500", "qty": "1", "action": "order"})
    client.post("/logout")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, oid)
        current_branch_id.set(wo.branch_id)
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == oid))
        part.urgent, part.status = True, "ordered"
        pu = Purchase(status="ordered", work_order_id=oid)
        db.add(pu)
        db.flush()
        db.add(PurchaseLine(purchase_id=pu.id, catalog_id=part.catalog_id, qty=1, price=1500, wo_part_id=part.id))
        db.commit()
        pid = pu.id

    def red(user):
        login(client, user, user + "123")
        b = [x for x in client.get("/api/notify").json()["badges"].get("/orders", []) if x["c"] == "red"]
        client.post("/logout")
        return b

    # без даты: менеджеру — «без срока», запчастисту — «не пришла»; ссылка ведёт прямо в наряд
    m = [x for x in red("irina") if "без срока" in x["t"]]
    assert m and m[0]["href"] == f"/orders/{oid}#parts" or m and m[0]["href"] == "/orders?tab=parts"
    assert any("не пришла" in x["t"] for x in red("oleg"))
    # запчастист поставил дату — у менеджера цифра гаснет, у запчастиста остаётся
    with SessionLocal() as db:
        db.get(Purchase, pid).expected_on = date.today()
        db.commit()
    assert not any("без срока" in x["t"] for x in red("irina"))
    assert any("не пришла" in x["t"] for x in red("oleg"))
    # деталь пришла — гаснет и у запчастиста
    with SessionLocal() as db:
        db.get(WoPart, part.id).status = "reserved"
        db.commit()
    assert not any("не пришла" in x["t"] for x in red("oleg"))


def test_badges_lead_to_the_task(client):
    """22.11.2026: кружок несёт наряды и блок — один наряд → ссылка сразу в него; несколько → список и каждый наряд."""
    login(client, "irina", "irina123")
    b = client.get("/api/notify").json()["badges"]
    for lst in b.values():
        for x in lst:
            assert "wo" in x and "sel" in x
            if len(x["wo"]) == 1 and x["blk"]:
                assert x["href"] == f"/orders/{x['wo'][0]}{x['blk']}"
    approve = next((x for x in b.get("/orders", []) if "согласовать" in x["t"]), None)
    if approve and len(approve["wo"]) > 1:
        html = client.get(approve["href"]).text
        assert all(f'href="/orders/{w}' in html for w in approve["wo"])
    client.post("/logout")
    # кладовщик: /api/notify не падает (ошибка 22.11 — локальный import в compute)
    login(client, "pavel", "pavel123")
    assert client.get("/api/notify").status_code == 200
    client.post("/logout")
    # запчастист: «черновики под наряды» ведут на существующую вкладку «Заказать»
    login(client, "oleg", "oleg123")
    for x in client.get("/api/notify").json()["badges"].get("/my-orders", []):
        assert client.get(x["href"]).status_code == 200 and "st=draft" not in x["href"]
    client.post("/logout")


def test_every_badge_has_its_tile(client):
    """23.11.2026 (брат): горит кружок — на его странице есть его плитка (по названию), и на ней не 0."""
    import re
    for who in ("irina", "oleg", "pavel", "andrey", "buh", "director"):
        login(client, who, who + "123")
        for menu, lst in client.get("/api/notify").json()["badges"].items():
            for x in lst:
                if not x.get("tile"):
                    continue
                page = menu if x["href"].count("/") == 2 and x["href"].startswith("/orders/") else x["href"].split("#")[0]
                html = client.get(page).text
                spans = re.findall(r'class="ptile[^"]*"[^>]*>\s*<span class="ptile-t">([^<]+)</span>\s*<b[^>]*>([^<]*)</b>', html)
                got = [int(re.sub(r"\D", "", n) or 0) for t, n in spans if t.strip() in x["tile"]]
                assert got and sum(got) > 0, (who, x["t"], x["tile"], spans)
        client.post("/logout")
    # новые плитки на страницах без плиток
    login(client, "buh", "buh123")
    html = client.get("/payroll/weeks").text
    assert all(t in html for t in ("Проверить", "Утвердить", "Выдать"))
    client.post("/logout")
    login(client, "director", "director123")
    html = client.get("/access-requests").text
    assert all(t in html for t in ("Ко всем нарядам", "К клиенту", "Открыть заново"))
    client.post("/logout")
    login(client, "pavel", "pavel123")
    assert "Заказ у поставщика" in client.get("/stock/leftovers").text
    client.post("/logout")
