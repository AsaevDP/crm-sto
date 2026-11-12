"""Новые мастера и мойка (12.11.2026): автоэлектрик, моторист, мойщик, детейлер; исполнитель у каждой работы,
«С автомобилем работали» в акте; химия на складе (раздел chem), закупка химии менеджером, запрос мойщика → выдача."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ChemRequest, PartCatalog, Purchase, PurchaseLine, StockItem, User, WoWork
from app.services import stock as ssvc

from .conftest import login
from .test_step3 import _create
from .test_step6 import _as, _wo


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _menu(client):
    return client.get("/more").text


def test_new_roles_and_menus(client):
    for lg, pwd in (("elektro", "elektro123"), ("motor", "motor123"), ("moyka", "moyka123"), ("detail", "detail123")):
        assert _uid(lg), lg
        login(client, lg, pwd)
        page = _menu(client)
        assert 'href="/orders"' in page
        if lg in ("moyka", "detail"):
            assert 'href="/wash/my"' in page and 'href="/razbor"' not in page and 'href="/stock"' not in page
        else:
            assert 'href="/stock"' in page and 'href="/wash/my"' not in page   # склад без цен, как у слесаря
        client.post("/logout")
    _as(client, "irina")
    page = _menu(client)
    assert 'href="/wash"' in page and 'href="/wash/my"' not in page
    _as(client, "oleg")
    assert 'href="/wash"' not in _menu(client)


def test_work_per_master_and_act(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КТ 1210 КТ", phone="0501210000", works="Замена ремня ГРМ").json()["url"]
    wo = _wo(url)
    page = client.get(url).text
    assert "Исполнитель" in page and "Автоэлектрик" in page and "Моторист" in page and "Мойщик" in page
    # Менеджер добавляет работы электрику и мойщику
    client.post(f"{url}/works", data={"name": "Диагностика проводки", "pricing": "fixed", "rate": "800", "mechanic_id": str(_uid("elektro"))})
    client.post(f"{url}/works", data={"name": "Мойка кузова", "pricing": "fixed", "rate": "500", "mechanic_id": str(_uid("moyka"))})
    with SessionLocal() as db:
        works = {w.name: w for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id))}
    assert works["Диагностика проводки"].mechanic_id == _uid("elektro")
    assert works["Мойка кузова"].mechanic_id == _uid("moyka")
    page = client.get(url).text
    assert "Работают с машиной" in page and "Автоэлектрик · Роман Электриков" in page
    act = client.get(f"{url}/act/works").text
    assert "С автомобилем работали" in act and "Автоэлектрик" in act and "Мойщик" in act
    # Электрик видит наряд со своей работой
    client.post("/logout")
    login(client, "elektro", "elektro123")
    assert client.get(url).status_code == 200
    assert "Мойка кузова" in client.get("/wash").text or client.get("/wash").status_code == 403
    client.post("/logout")
    # Мойщик видит машину в «Мойке» у менеджера
    _as(client, "irina")
    assert "КТ 1210 КТ" in client.get("/wash?tab=now").text


def test_chem_purchase_ask_issue(client):
    # Менеджер заказывает химию (закупка kind=chem) — только такую он может править
    _as(client, "irina")
    r = client.post("/wash/purchases/new", data={"supplier_new": "Химия (тест)"})
    pid = uuid.UUID(r.url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid}/lines", data={"name": "Шампунь для мойки (тест)", "qty": "5", "price": "200"})
    client.post(f"/purchases/{pid}/ordered", data={})
    with SessionLocal() as db:
        p = db.get(Purchase, pid)
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.purchase_id == pid))
        cat = db.get(PartCatalog, line.catalog_id)
        assert p.kind == "chem" and p.status == "ordered" and cat.section == "chem" and cat.category == "Шампуни"
        other = db.scalar(select(Purchase).where(Purchase.kind != "chem"))
    if other:   # обычную закупку менеджер не правит
        assert client.post(f"/purchases/{other.id}", data={"action": "save"}).status_code == 403
    # Кладовщик принимает
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/delivered", data={})
    client.post(f"/purchases/{pid}/receive", data={"all": str(pid)})
    with SessionLocal() as db:
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id)).all()
        free = sum(b["free"] for b in ssvc.balances(db, items[0].branch_id, [i.id for i in items]).values())
    assert free == 5
    assert "Шампунь для мойки (тест)" in client.get("/stock?sec=chem").text
    assert "Химия для ухода за авто" in client.get("/stock").text
    # Мойщик просит 2
    client.post("/logout")
    login(client, "moyka", "moyka123")
    assert "Шампунь для мойки (тест)" in client.get("/wash/my").text
    client.post("/wash/ask", data={"catalog_id": str(cat.id), "qty": "2", "note": "на пеногенератор"})
    with SessionLocal() as db:
        req = db.scalar(select(ChemRequest).where(ChemRequest.catalog_id == cat.id))
        assert req.status == "new" and req.qty == 2
    assert client.post(f"/chem/{req.id}/issue", data={}).status_code == 403   # мойщик сам себе не выдаёт
    client.post("/logout")
    # Кладовщик видит во вкладке «Химия» и выдаёт → остаток 3, в счёт наряда не идёт
    _as(client, "pavel")
    page = client.get("/stock/issues?tab=chem").text
    assert "Шампунь для мойки (тест)" in page and "на пеногенератор" in page
    client.post(f"/chem/{req.id}/issue", data={"qty": "2"})
    with SessionLocal() as db:
        assert db.get(ChemRequest, req.id).status == "issued"
        free = sum(b["free"] for b in ssvc.balances(db, items[0].branch_id, [i.id for i in items]).values())
    assert free == 3
    # Больше, чем есть, — не выдаём
    _as(client, "irina")
    page = client.get("/wash?tab=chem").text
    assert "Артём Мойкин" in page and "Кто сколько взял" in page


def test_second_master_can_request_tool(client):
    """12.11.2026: второй мастер на машине (электрик со своей работой) просит общий инструмент под наряд."""
    from app.models import Tool, ToolRequest
    _as(client, "pavel")
    client.post("/tools", data={"name": "Мультиметр (тест 2-й мастер)", "category": "electric", "cost": "3000"})
    with SessionLocal() as db:
        tool = db.scalar(select(Tool).where(Tool.name == "Мультиметр (тест 2-й мастер)"))
    from .test_step6 import _order_to_work
    url = _order_to_work(client, "КТ 1211 КТ", "0501211000", "Колодки (инструмент)")
    _as(client, "irina")
    client.post(f"{url}/works", data={"name": "Диагностика проводки", "pricing": "fixed", "rate": "600",
                                      "mechanic_id": str(_uid("elektro"))})
    client.post("/logout")
    login(client, "moyka", "moyka123")   # без своей работы в наряде — нельзя
    assert client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)}).status_code == 403
    client.post("/logout")
    login(client, "elektro", "elektro123")
    assert "Попросить инструмент" in client.get(url).text
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    with SessionLocal() as db:
        r = db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.status == "new"))
        w = db.get(WoWork, r.wo_work_id) if r and r.wo_work_id else None
    assert r and r.user_id == _uid("elektro") and w and w.name == "Диагностика проводки"
    client.post("/logout")
    _as(client, "pavel")
    client.post(f"/tool-requests/{r.id}/done", data={"result": "ok"})
    with SessionLocal() as db:
        assert db.get(Tool, tool.id).holder_id == _uid("elektro")


def test_new_finding_refused_back_to_work(client):
    """Новая находка во время ремонта, клиент отказался → снова «В работе», а не «Подбор» (живой прогон 12.11.2026)."""
    from .test_step6 import _order_to_work
    url = _order_to_work(client, "КТ 1212 КТ", "0501212000", "Колодки (находка)")
    wo = _wo(url)
    mech = {u: l for l, u in ((lg, _uid(lg)) for lg in ("andrey", "sergey", "viktor"))}.get(wo.mechanic_id, "admin")
    _as(client, "admin")
    client.post(f"{url}/findings", data={"text": "Течь сальника (во время работы)", "urgency": "soon"})
    assert _wo(url).status == "approve"
    from app.models import WoFinding
    with SessionLocal() as db:
        f = db.scalar(select(WoFinding).where(WoFinding.work_order_id == wo.id, WoFinding.approval == "pending"))
    _as(client, "irina")
    client.post(f"{url}/findings/{f.id}/approval", data={"value": "no"})
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "work"
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.status != "done"))
    _as(client, "admin")
    assert client.post(f"{url}/works/{w.id}/state", data={"action": "done"}).status_code in (200, 303)


def test_second_master_works_while_parts_wait(client):
    """Мойщик моет, пока ведущий ждёт запчасти; ведущий начать не может, пока запчасти не выданы."""
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КТ 1213 КТ", phone="0501213000", works="Замена амортизаторов").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": "Амортизатор (ждём)", "qty": "2", "cost": "2000", "action": "order"})
    client.post(f"{url}/picking-done")
    assert _wo(url).status == "waiting"
    _as(client, "irina")
    client.post(f"{url}/works", data={"name": "Мойка кузова", "pricing": "fixed", "rate": "350", "mechanic_id": str(_uid("moyka"))})
    with SessionLocal() as db:
        works = {w.name: w for w in db.scalars(select(WoWork).where(WoWork.work_order_id == _wo(url).id))}
    client.post("/logout")
    login(client, "moyka", "moyka123")
    assert "Начал" in client.get(url).text
    assert client.post(f"{url}/works/{works['Мойка кузова'].id}/state", data={"action": "start"}).status_code in (200, 303)
    client.post(f"{url}/works/{works['Мойка кузова'].id}/state", data={"action": "done"})
    assert _wo(url).status == "waiting"   # машина всё ещё ждёт запчасти ведущего
    client.post("/logout")
    lead = _wo(url).mechanic_id
    lead_login = next(lg for lg in ("andrey", "sergey") if _uid(lg) == lead) if lead else None
    if lead_login:
        login(client, lead_login, lead_login + "123")
        assert client.post(f"{url}/works/{works['Замена амортизаторов'].id}/state", data={"action": "start"}).status_code == 403


def test_dismiss_requires_transfer(client):
    from app.models import Role
    _as(client, "admin")
    with SessionLocal() as db:
        rid = db.scalar(select(Role.id).where(Role.code == "mechanic"))
    client.post("/settings/users", data={"full_name": "Тимур Временный", "login": "temp.mech", "role_id": str(rid),
                                         "password": "temp123", "pin": ""})
    tid = _uid("temp.mech")
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КТ 1214 КТ", phone="0501214000", works="ТО",
                  mechanic_id=str(tid)).json()["url"]
    wo = _wo(url)
    _as(client, "director")
    r = client.post(f"/settings/users/{tid}/dismiss", data={"reason": "тест"})
    assert "Сначала передайте открытые наряды" in r.text
    with SessionLocal() as db:
        assert db.get(User, tid).active
    client.post(f"/settings/users/{tid}/dismiss", data={"reason": "тест", f"wo_{wo.id}_mechanic_id": str(_uid("andrey"))})
    with SessionLocal() as db:
        assert not db.get(User, tid).active
    assert _wo(url).mechanic_id == _uid("andrey")


def test_stock_to_order_by_partsman(client):
    """12.11.2026: запчастист со склада ставит позицию в свой открытый наряд — сразу в резерв."""
    from decimal import Decimal
    from app.models import PartCatalog, StockItem, WoPart
    from app.services import stock as ssvc
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КТ 1401 КТ", phone="0501401000", works="ТО").json()["url"]
    wo = _wo(url)
    with SessionLocal() as db:
        from app.db import current_branch_id
        current_branch_id.set(wo.branch_id)
        c = ssvc.get_or_create_catalog(db, "Фильтр (в наряд со склада)", "TST-1", "MANN")
        item = ssvc.get_or_create_item(db, c.id, None)
        item.cost = Decimal("200")
        ssvc.move(db, "in", item, 3, comment="тест")
        db.commit()
        item_id, cat_id = item.id, c.id
    _as(client, "oleg")
    page = client.get("/stock?sec=to").text
    assert "+ В наряд" in page and wo.number[-6:] in page
    client.post(f"/stock/items/{item_id}/to-order", data={"order_id": str(wo.id), "qty": "2", "back": "/stock"})
    with SessionLocal() as db:
        p = db.scalar(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.catalog_id == cat_id))
        assert p and p.status == "reserved" and p.qty == 2 and p.price > p.cost
    r = client.post(f"/stock/items/{item_id}/to-order", data={"order_id": str(wo.id), "qty": "5", "back": "/stock"})
    assert "Свободно только 1" in r.text
    _as(client, "andrey")   # не запчастист — нельзя
    assert client.post(f"/stock/items/{item_id}/to-order", data={"order_id": str(wo.id), "qty": "1"}).status_code == 403
