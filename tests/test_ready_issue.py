"""«Готово к выдаче» для инструмента и химии (19.11.2026, брат) — как у запчастей: кладовщик «📦 Готово к выдаче»
(у того, кто просил, — зелёная цифра у «Моих запросов» и сообщение) → «Выдано», когда пришли; или «Выдано сразу».
Химия при «Готово» откладывается (резерв): не хватает — «Готово» не нажимается; отказ / отмена — резерв снимается."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ChemRequest, Notification, PartCatalog, Purchase, PurchaseLine, StockItem, Tool, ToolRequest, User, WorkOrder
from app.services import stock as ssvc

from .conftest import login
from .test_step3 import _create
from .test_tool_requests import _new_shared_tool

FETCH = {"x-requested-with": "fetch"}


def _badges(client):
    return client.get("/api/notify").json()["badges"].get("/my-requests", [])


def _chem_in_stock(client, name, qty):
    login(client, "oleg", "oleg123")   # 20.11.2026: химию заказывает запчастист
    r = client.post("/wash/purchases/new", data={"supplier_new": "Химия (готово)"})
    pid = uuid.UUID(r.url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid}/lines", data={"name": name, "qty": str(qty), "price": "150"})
    client.post(f"/purchases/{pid}/ordered", data={})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-77", "all": str(pid)})
    client.post("/logout")
    with SessionLocal() as db:
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.purchase_id == pid))
        return db.get(PartCatalog, line.catalog_id)


def _free(cat_id):
    with SessionLocal() as db:
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat_id)).all()
        return sum((b["free"] for b in ssvc.balances(db, items[0].branch_id, [i.id for i in items]).values()), Decimal(0))


def test_chem_ready_reserves_then_issued(client):
    cat = _chem_in_stock(client, "Пена (готово к выдаче)", 3)
    login(client, "moyka", "moyka123")
    client.post("/wash/ask", data={"catalog_id": str(cat.id), "qty": "2"})
    with SessionLocal() as db:
        req = db.scalar(select(ChemRequest).where(ChemRequest.catalog_id == cat.id))
    assert any(x["c"] == "orange" and "химия" in x["t"] for x in _badges(client))
    client.post("/logout")

    login(client, "pavel", "pavel123")
    page = client.get("/stock/issues?tab=chem").text
    assert "📦 Готово к выдаче" in page and "Выдано сразу" in page
    # больше, чем есть, — «Готово» не нажимается
    client.post(f"/chem/{req.id}/ready", data={"qty": "5"})
    with SessionLocal() as db:
        assert db.get(ChemRequest, req.id).status == "new"
    client.post(f"/chem/{req.id}/ready", data={"qty": "2"})
    with SessionLocal() as db:
        r = db.get(ChemRequest, req.id)
        assert r.status == "ready" and r.issued_qty == 2
    assert _free(cat.id) == 1   # отложено 2 из 3
    assert "Готово — ждут, когда заберут" in client.get("/stock/issues?tab=chem").text
    client.post("/logout")

    # у мойщика — зелёная цифра и сообщение «готово, подойдите»
    login(client, "moyka", "moyka123")
    b = _badges(client)
    assert any(x["c"] == "red" and "химия готова" in x["t"] for x in b)   # 23.11.2026: «готово — забрать» — 🔴
    assert not any(x["c"] == "orange" and "химия" in x["t"] for x in b)
    with SessionLocal() as db:
        moyka = db.scalar(select(User).where(User.login == "moyka"))
        assert db.scalar(select(Notification).where(Notification.user_id == moyka.id, Notification.kind == "chem_ready"))
    assert "готово — у окошка кладовщика" in client.get("/my-requests?tab=chem").text
    client.post("/logout")

    # пришёл — «Выдано»: со склада ушло 2, резерв снят, зелёная гаснет
    login(client, "pavel", "pavel123")
    client.post(f"/chem/{req.id}/issue", data={})
    with SessionLocal() as db:
        r = db.get(ChemRequest, req.id)
        assert r.status == "issued" and r.issued_qty == 2
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id)).all()
        bal = ssvc.balances(db, items[0].branch_id, [i.id for i in items])
        assert sum(b["on_hand"] for b in bal.values()) == 1 and sum(b["reserved"] for b in bal.values()) == 0
    client.post("/logout")
    login(client, "moyka", "moyka123")
    assert not any("химия готова" in x["t"] for x in _badges(client))
    client.post("/logout")


def test_chem_ready_then_declined_or_cancelled_returns_to_shelf(client):
    cat = _chem_in_stock(client, "Воск (отказ после готово)", 2)
    login(client, "detail", "detail123")
    client.post("/wash/ask", data={"catalog_id": str(cat.id), "qty": "1"})
    client.post("/wash/ask", data={"catalog_id": str(cat.id), "qty": "1"})
    client.post("/logout")
    with SessionLocal() as db:
        a, b = db.scalars(select(ChemRequest).where(ChemRequest.catalog_id == cat.id).order_by(ChemRequest.created_at)).all()
    login(client, "pavel", "pavel123")
    client.post(f"/chem/{a.id}/ready", data={"qty": "1"})
    client.post(f"/chem/{b.id}/ready", data={"qty": "1"})
    assert _free(cat.id) == 0
    client.post(f"/chem/{a.id}/decline", data={"reason": "Нашлась початая"})
    assert _free(cat.id) == 1
    client.post("/logout")
    login(client, "detail", "detail123")
    client.post(f"/wash/requests/{b.id}/cancel")
    client.post("/logout")
    assert _free(cat.id) == 2
    with SessionLocal() as db:
        assert db.get(ChemRequest, a.id).status == "declined" and db.get(ChemRequest, b.id).status == "cancelled"


def test_tool_ready_then_issued(client):
    tool = _new_shared_tool(client, "Съёмник пружин (готово)")
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 9191 КА", phone="0509191000").json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    assert any(x["c"] == "orange" and "инструмент" in x["t"] for x in _badges(client))
    client.post("/logout")
    with SessionLocal() as db:
        rid = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id))

    login(client, "pavel", "pavel123")
    page = client.get("/stock/issues?tab=tools").text
    assert "📦 Готово к выдаче" in page and "Выдано сразу" in page
    assert client.post(f"/tool-requests/{rid}/ready", headers=FETCH).json()["ok"]
    with SessionLocal() as db:
        assert db.get(ToolRequest, rid).status == "ready" and db.get(Tool, tool.id).holder_id is None
    assert "Готово — ждут, когда заберут" in client.get("/stock/issues?tab=tools").text
    client.post("/logout")

    login(client, "andrey", "andrey123")
    assert any(x["c"] == "red" and "инструмент готов" in x["t"] for x in _badges(client))
    assert "готово — у кладовщика" in client.get(url).text
    assert "готово — у окошка кладовщика" in client.get("/my-requests?tab=tools").text
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})   # пока готово — второй раз не попросить
    with SessionLocal() as db:
        assert len(db.scalars(select(ToolRequest).where(ToolRequest.tool_id == tool.id)).all()) == 1
    client.post("/logout")

    login(client, "pavel", "pavel123")
    client.post(f"/tool-requests/{rid}/done")
    with SessionLocal() as db:
        andrey = db.scalar(select(User).where(User.login == "andrey"))
        assert db.get(ToolRequest, rid).status == "done" and db.get(Tool, tool.id).holder_id == andrey.id
        assert not db.scalar(select(Notification).where(Notification.user_id == andrey.id, Notification.kind == "tool_ready",
                                                        Notification.read_at.is_(None)))
    client.post("/logout")
    login(client, "andrey", "andrey123")
    assert not any("инструмент готов" in x["t"] for x in _badges(client))
    client.post(f"/tools/{tool.id}/return-request")   # вернуть в шкаф — база в тестах общая
    client.post("/logout")
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rr = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id, ToolRequest.status == "new"))
    client.post(f"/tool-requests/{rr}/done")
    with SessionLocal() as db:
        assert db.get(Tool, tool.id).holder_id is None
    client.post("/logout")


def test_manager_picks_up_tool_for_master(client):
    """19.11.2026 (брат): инструмент — как запчасти. Слесарь попросил → у кладовщика 🔴 у «Инструмента», у менеджера
    наряда 🟠 у «Запросов на выдачу». Кладовщик «Готово» → у кладовщика 🟠 (не выдано), у менеджера 🟢 и карточка
    с «Выдано» → менеджер забрал и отдал → инструмент на руках у слесаря, цифры погасли. У «Инструмента» менеджера цифр нет."""
    tool = _new_shared_tool(client, "Съёмник (менеджер видит)")
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 6060 КА", phone="0506060000").json()["url"]
    client.post("/logout")
    with SessionLocal() as db:
        from app.db import current_branch_id
        wo = db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))
        current_branch_id.set(wo.branch_id)
        wo.mechanic_id, wo.manager_id = db.scalar(select(User.id).where(User.login == "andrey")), db.scalar(select(User.id).where(User.login == "irina"))
        db.commit()
    login(client, "andrey", "andrey123")
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    client.post("/logout")
    with SessionLocal() as db:
        rid = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id))

    login(client, "irina", "irina123")
    b = client.get("/api/notify").json()["badges"]
    assert any(x["c"] == "orange" and "инструмент" in x["t"] for x in b.get("/ready", []))
    assert not b.get("/tools")   # сиреневую «для сведения» убрали
    page = client.get("/ready").text
    assert "Съёмник (менеджер видит)" in page and f"/tool-requests/{rid}/pickup" not in page
    client.post(f"/tool-requests/{rid}/pickup")   # пока не подготовлено — нельзя
    with SessionLocal() as db:
        assert db.get(ToolRequest, rid).status == "new"
    client.post("/logout")

    login(client, "pavel", "pavel123")
    assert any(x["c"] == "red" for x in client.get("/api/notify").json()["badges"].get("/tools", []))
    client.post(f"/tool-requests/{rid}/ready")
    b = client.get("/api/notify").json()["badges"].get("/tools", [])
    assert any(x["c"] == "orange" and "подготовлен" in x["t"] for x in b)
    assert "готово — не выдано" in client.get("/stock/issues?tab=tools").text
    client.post("/logout")

    login(client, "irina", "irina123")
    b = client.get("/api/notify").json()["badges"]
    assert any(x["c"] == "orange" and "забрать у окошка" in x["t"] for x in b.get("/ready", []))   # 23.11.2026: 🟠 (было 🟢)
    assert not b.get("/tools")
    page = client.get("/ready").text
    assert f"/tool-requests/{rid}/pickup" in page and "Отнести мастеру" in page
    client.post(f"/tool-requests/{rid}/pickup")
    with SessionLocal() as db:
        andrey = db.scalar(select(User).where(User.login == "andrey"))
        assert db.get(ToolRequest, rid).status == "done" and db.get(Tool, tool.id).holder_id == andrey.id
    assert not any("инструмент" in x["t"] for x in client.get("/api/notify").json()["badges"].get("/ready", []))
    assert "Недавно выдано" in client.get("/ready").text
    client.post("/logout")

    login(client, "pavel", "pavel123")
    assert not any("подготовлен" in x["t"] for x in client.get("/api/notify").json()["badges"].get("/tools", []))
    client.post("/logout")
    login(client, "andrey", "andrey123")   # вернуть в шкаф — база в тестах общая
    client.post(f"/tools/{tool.id}/return-request")
    client.post("/logout")
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rr = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id, ToolRequest.status == "new"))
    client.post(f"/tool-requests/{rr}/done")
    client.post("/logout")