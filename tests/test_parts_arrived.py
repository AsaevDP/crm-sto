"""Пришли запчасти (12.11.2026): кладовщик принял часть запчастей наряда → «Пришли запчасти: N» менеджеру,
запчастисту и ВСЕМ мастерам наряда (в одном заказе бывают детали для разных мастеров). У каждого в меню у «Нарядов»
зелёная цифра — сколько запчастей пришло и ждёт выдачи; гаснет, когда кладовщик выдал."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import IssueRequest, Location, Notification, PurchaseLine, User, WoPart, WorkOrder, WoWork

from .conftest import agree, login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}
TEAM = (("irina", "irina123"), ("oleg", "oleg123"), ("andrey", "andrey123"), ("elektro", "elektro123"))


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _kinds(login_, url):
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == login_))
        return [(n.kind, n.title, n.text) for n in db.scalars(select(Notification).where(
            Notification.user_id == uid, Notification.work_order_id == _wo(url).id))]


def _arrived_badge(client, who, pwd, url):
    """Сколько пришедших запчастей по ЭТОМУ наряду (база в тестах общая); заодно цифра в меню = сумма по всем нарядам."""
    from app.services.notify import arrived_waiting
    login(client, who, pwd)
    b = client.get("/api/notify", headers=FETCH).json()["badges"].get("/orders", [])
    client.post("/logout")
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == who))
        per_wo = arrived_waiting(db, u.branch_id, u.id)
    assert sum(x["n"] for x in b if x["href"] == "/orders?tab=arrived") == sum(per_wo.values()), who
    return per_wo.get(_wo(url).id, 0)


def test_arrived_parts_notify_all_masters_and_badge(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 8123 КА", phone="0508123000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    for name in ("Свеча (тест «пришли»)", "Датчик (тест «пришли»)"):
        client.post(f"{url}/parts", data={"name": name, "qty": "1", "cost": "100", "markup_pct": "30", "action": "order"})
    agree(client, url)   # 13.11.2026: подбор завершён → менеджер согласовал → заказ
    client.post("/logout")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, _wo(url).id)
        current_branch_id.set(wo.branch_id)
        wo.mechanic_id = db.scalar(select(User.id).where(User.login == "andrey"))
        elektro = db.scalar(select(User.id).where(User.login == "elektro"))
        db.add(WoWork(work_order_id=wo.id, branch_id=wo.branch_id, name="Электрика (тест)", mechanic_id=elektro))
        parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id)).all()
        sensor = next(p for p in parts if p.name.startswith("Датчик"))
        line = db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == sensor.id))
        pid, line_id = line.purchase_id, line.id
        db.commit()
    assert _wo(url).status == "waiting"
    login(client, "oleg", "oleg123")
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    client.post("/logout")

    # Кладовщик принял одну деталь из двух → всем четверым «Пришли запчасти: 1 шт.», на складе 1 из 2
    login(client, "pavel", "pavel123")
    client.post("/stock/locations", data={"rack": "P", "shelves": "1", "places": "1"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "P"))
    client.post("/stock/receive", data={"line_id": str(line_id), f"loc_{line_id}": str(loc.id)})
    client.post("/logout")
    assert _wo(url).status == "waiting"
    for who, _ in TEAM:
        got = [n for n in _kinds(who, url) if n[0] == "parts_in"]
        assert len(got) == 1, who
        assert "Пришли запчасти: 1 шт." in got[0][1] and "на складе 1 из 2" in got[0][2], who
    for who, pwd in TEAM:
        assert _arrived_badge(client, who, pwd, url) == 1, who

    # Плитка-фильтр «Пришли запчасти» в нарядах
    login(client, "elektro", "elektro123")
    page = client.get("/orders?tab=arrived").text
    assert "Пришли запчасти" in page and _wo(url).number in page
    client.post("/logout")

    # Пришло остальное → «Все запчасти на складе» всем четверым (и электрику), отдельного «Пришли» нет
    login(client, "pavel", "pavel123")
    client.post("/stock/receive", data={"all": str(pid)})
    client.post("/logout")
    assert _wo(url).status == "instock"
    for who, pwd in TEAM:
        kinds = [k for k, _, _ in _kinds(who, url)]
        assert kinds.count("parts_ready") == 1 and kinds.count("parts_in") == 1, who
        assert _arrived_badge(client, who, pwd, url) == 2, who

    # Кладовщик выдал → зелёная цифра гаснет
    login(client, "andrey", "andrey123")
    client.post(f"{url}/issue-request")
    client.post("/logout")
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id, IssueRequest.status == "new"))
    client.post(f"/stock/issues/{rid}/issue")
    client.post("/logout")
    for who, pwd in TEAM:
        assert _arrived_badge(client, who, pwd, url) == 0, who
