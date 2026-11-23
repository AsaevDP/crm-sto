"""23.11.2026 (брат): согласование по шагам — Работы (менеджер) → Подбор (запчастист) → Звонок клиенту (менеджер).
Кружки загораются по очереди: менеджеру 🔴 «впишите работы», запчастисту 🟠 «скоро подбор» → запчастисту 🔴 «подобрать»
→ менеджеру 🔴 «звоните клиенту». После «Согласовал» кладовщику 🟠 «подготовить к выдаче»; 🟣 «отложено со склада» —
с подбора до «Получил» мастера."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import IssueRequest, Notification, User, WorkOrder, WoWork

from .conftest import login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _badges(c, who):
    c.post("/logout")
    login(c, who, who + "123")
    b = c.get("/api/notify", headers=FETCH).json()["badges"]
    return b


def _has(b, menu, color, text, wo_id=None):
    for x in b.get(menu, []):
        for g in x.get("go") or [x]:
            if x["c"] == color and text in g["t"] and (wo_id is None or str(wo_id) in x.get("wo", []) or not x.get("wo")):
                return True
    return False


def _notes(who, wo_id, kind):
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == who))
        return db.scalars(select(Notification).where(Notification.user_id == uid, Notification.work_order_id == wo_id,
                                                     Notification.kind == kind)).all()


def test_steps_works_picking_call_and_keeper(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 2311 КА", phone="0502311000").json()["url"]
    wid = _wo(url).id
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Колодки стёрты", "urgency": "urgent"}, headers=FETCH)
    client.post(f"{url}/diag-done")
    assert _wo(url).status == "approve" and _wo(url).works_sent_at is None

    # Шаг «Работы»: менеджеру 🔴 и окошко; запчастисту 🟠 «скоро подбор», подбора нет
    assert _has(_badges(client, "irina"), "/orders", "red", "впишите работы", wid)
    assert _notes("irina", wid, "works_todo")
    page = client.get(url).text
    assert 'id="btn-works-sent"' in page and "Работы записаны → на подбор" in page
    assert page.index('id="works"') < page.index('id="parts"')          # «Работы» выше «Запчастей»
    b = _badges(client, "oleg")
    assert _has(b, "/orders", "orange", "скоро подбор", wid) and not _has(b, "/orders", "red", "подобрать запчасти", wid)
    page = client.get(url).text
    assert "Менеджер заполняет работы" in page and 'id="btn-picked"' not in page
    client.post(f"{url}/parts", data={"name": "Колодки (рано)", "qty": "1", "cost": "100", "markup_pct": "30", "action": "order"})
    with SessionLocal() as db:
        from app.models import WoPart
        assert not db.scalars(select(WoPart).where(WoPart.work_order_id == wid)).all()   # до работ — не добавить

    # Менеджер пишет работу и отправляет на подбор
    client.post("/logout")
    login(client, "irina", "irina123")
    assert "Добавьте хотя бы одну работу" in client.post(f"{url}/works-sent").text
    client.post(f"{url}/works", data={"name": "Замена колодок", "pricing": "fixed", "rate": "800", "qty": "1"})
    client.post(f"{url}/works-sent")
    assert _wo(url).works_sent_at is not None
    b = _badges(client, "irina")
    assert not _has(b, "/orders", "red", "впишите работы", wid) and not _has(b, "/orders", "red", "звоните", wid)
    b = _badges(client, "oleg")
    assert _has(b, "/orders", "red", "подобрать запчасти", wid) and not _has(b, "/orders", "orange", "скоро подбор", wid)
    assert _notes("oleg", wid, "works_sent")

    # Подбор: одна деталь со склада (если есть свободная) и одна под заказ
    pick = client.get("/api/stock/pick", headers=FETCH).json()["items"]
    stock_item = next((x for x in pick if x["free"] >= 1 and not x.get("used")), None)
    if stock_item:
        client.post(f"{url}/parts", data={"catalog_id": stock_item["id"], "name": stock_item["name"], "qty": "1",
                                          "cost": str(stock_item["cost"] or 100), "markup_pct": "30", "action": "auto"})
    client.post(f"{url}/parts", data={"name": "Датчик износа (шаги)", "qty": "1", "cost": "100", "markup_pct": "30",
                                      "action": "order"})
    if stock_item:   # кладовщику 🟣 для сведения — отложено со склада
        assert _has(_badges(client, "pavel"), "/stock", "purple", "отложено со склада")
        client.post("/logout")
        login(client, "oleg", "oleg123")
    client.post(f"{url}/picking-done")
    assert _wo(url).picked_at is not None

    # Звонок: менеджеру 🔴 «звоните клиенту», окошко «Подбор готов»; у работы «Да / Нет»
    b = _badges(client, "irina")
    assert _has(b, "/orders", "red", "звоните клиенту", wid)
    assert _notes("irina", wid, "picked")
    page = client.get(url).text
    assert "✓ Да, делаем" in page and 'id="btn-approve-done"' in page
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wid))
    assert "Отметьте по каждой работе" in client.post(f"{url}/approval-done").text
    client.post(f"{url}/works/{w.id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status in ("picking", "waiting", "instock")
    assert not _has(_badges(client, "irina"), "/orders", "red", "звоните клиенту", wid)

    if stock_item:
        # Кладовщику 🟠 «подготовить к выдаче» + окошко; 🟣 ещё горит
        b = _badges(client, "pavel")
        assert _has(b, "/stock", "orange", "подготовить к выдаче") and _has(b, "/stock", "purple", "отложено со склада")
        assert _notes("pavel", wid, "prepare")
        page = client.get("/stock/reserved").text
        assert 'data-prep="1"' in page and "Подготовить к выдаче" in page
        # Мастер запросил выдачу — 🟠 гаснет, 🟣 остаётся; выдали — остаётся; «Получил» — гаснет
        client.post("/logout")
        login(client, "andrey", "andrey123")
        client.post(f"{url}/issue-request")
        from app.services import keeper_watch as kw
        with SessionLocal() as db:
            pv = db.scalar(select(User).where(User.login == "pavel"))
            assert wid not in kw.prepare(db, pv.branch_id, ("new",)) and wid in kw.held(db, pv.branch_id, ("new",))
            rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == wid, IssueRequest.status == "new"))
        client.post("/logout")
        login(client, "pavel", "pavel123")
        client.post(f"/stock/issues/{rid}/issue")
        with SessionLocal() as db:
            assert wid in kw.held(db, pv.branch_id, ("new",))
        client.post("/logout")
        login(client, "andrey", "andrey123")
        client.post(f"/stock/issues/{rid}/received")
        with SessionLocal() as db:
            assert wid not in kw.held(db, pv.branch_id, ("new",))


def test_no_parts_skips_picking(client):
    """«Работы записаны — запчасти не нужны» — подбор пропускается, сразу звонок; запчастисту ничего не горит."""
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 2312 КА", phone="0502312000").json()["url"]
    wid = _wo(url).id
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Скрип ремня", "urgency": "soon"}, headers=FETCH)
    client.post(f"{url}/diag-done")
    client.post("/logout")
    login(client, "irina", "irina123")
    client.post(f"{url}/works", data={"name": "Подтяжка ремня", "pricing": "fixed", "rate": "300", "qty": "1"})
    client.post(f"{url}/works-sent", data={"no_parts": "1"})
    assert _has(_badges(client, "irina"), "/orders", "red", "звоните клиенту", wid)
    b = _badges(client, "oleg")
    assert not _has(b, "/orders", "red", "подобрать запчасти", wid) and not _has(b, "/orders", "orange", "скоро подбор", wid)


def test_new_problem_group_new(client):
    """Новая проблема во время ремонта — тот же круг; новые работы — группа «Новое», старые продолжаются."""
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 2313 КА", phone="0502313000").json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Течь масла", "urgency": "soon"}, headers=FETCH)
    client.post(f"{url}/diag-done")
    client.post("/logout")
    login(client, "irina", "irina123")
    client.post(f"{url}/works", data={"name": "Замена прокладки", "pricing": "fixed", "rate": "500", "qty": "1"})
    client.post(f"{url}/works-sent", data={"no_parts": "1"})
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == _wo(url).id))
    client.post(f"{url}/works/{w.id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "work"
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Порван пыльник", "urgency": "urgent"}, headers=FETCH)
    assert _wo(url).status == "approve" and _wo(url).works_sent_at is None
    client.post("/logout")
    login(client, "irina", "irina123")
    client.post(f"{url}/works", data={"name": "Замена пыльника", "pricing": "fixed", "rate": "400", "qty": "1"})
    page = client.get(url).text
    block = page[page.index('id="works"'):page.index('id="parts"')]
    assert "Новое · добавлено" in block
    assert block.index("Замена прокладки") < block.index("Новое · добавлено") < block.index("Замена пыльника")
