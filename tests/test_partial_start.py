"""Работа с частью запчастей (19.11.2026, брат): одна деталь на складе, вторая едет («Ждём запчасти») — слесарь
запрашивает выдачу того, что есть, начинает и закрывает отдельные работы; последнюю работу закрыть нельзя, пока не
выдано всё (у менеджера не должно выглядеть «всё сделано»). Приехало остальное → выдали → последнюю закрыл → «Готово»."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import IssueRequest, User, WoPart, WorkOrder, WoWork

from .conftest import login
from .test_step3 import _create


def _wo(oid):
    with SessionLocal() as db:
        return db.get(WorkOrder, oid)


def _st(wid):
    with SessionLocal() as db:
        return db.get(WoWork, wid).status


def test_start_with_part_of_parts_but_not_finish_all(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 5858 КА", phone="0505858000").json()["url"]
    client.post("/logout")
    oid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        wo = db.get(WorkOrder, oid)
        current_branch_id.set(wo.branch_id)
        andrey = db.scalar(select(User.id).where(User.login == "andrey"))
        wo.mechanic_id, wo.status = andrey, "waiting"
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == oid)):
            db.delete(w)
        w1 = WoWork(work_order_id=oid, branch_id=wo.branch_id, name="Снять рычаг (тест)", mechanic_id=andrey)
        w2 = WoWork(work_order_id=oid, branch_id=wo.branch_id, name="Поставить шаровую (тест)", mechanic_id=andrey)
        here = WoPart(work_order_id=oid, branch_id=wo.branch_id, name="Рычаг (на складе)", qty=1, price=500, status="reserved")
        coming = WoPart(work_order_id=oid, branch_id=wo.branch_id, name="Шаровая (едет)", qty=1, price=300, status="ordered")
        db.add_all([w1, w2, here, coming])
        db.commit()
        w1_id, w2_id, coming_id = w1.id, w2.id, coming.id

    login(client, "andrey", "andrey123")
    page = " ".join(client.get(url).text.split())
    assert "Запросить выдачу того, что есть" in page and "Ещё нет на складе: Шаровая (едет)" in page
    # пока ничего не выдано — начать нельзя
    assert client.post(f"{url}/works/{w1_id}/state", data={"action": "start"}).status_code == 403
    client.post(f"{url}/issue-request", data={"comment": "начну с рычага"})
    client.post("/logout")
    with SessionLocal() as db:
        req = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == oid))
    assert req.status == "new"

    login(client, "pavel", "pavel123")
    client.post(f"/stock/issues/{req.id}/issue")
    client.post("/logout")
    assert _wo(oid).status == "waiting"   # остальное ещё едет — этап прежний

    # слесарь работает с тем, что есть: первую работу начал и закрыл
    login(client, "andrey", "andrey123")
    # 20.11.2026: выдали — но пока не нажал «Получил», «Начал» серая и не срабатывает
    page = client.get(url).text
    assert "✓ Получил" in page and "Сначала подтвердите" in page
    client.post(f"{url}/works/{w1_id}/state", data={"action": "start"})
    assert _st(w1_id) == "todo"
    client.post(f"/stock/issues/{req.id}/received")
    assert "Сначала подтвердите" not in client.get(url).text
    client.post(f"{url}/works/{w1_id}/state", data={"action": "start"})
    client.post(f"{url}/works/{w1_id}/state", data={"action": "done"})
    assert _st(w1_id) == "done"
    # последнюю — начать можно, закрыть нельзя
    client.post(f"{url}/works/{w2_id}/state", data={"action": "start"})
    r = client.post(f"{url}/works/{w2_id}/state", data={"action": "done"})
    assert _st(w2_id) == "doing" and "последняя работа" in r.text
    assert _wo(oid).status == "waiting"
    client.post("/logout")

    # приехала шаровая → на складе → выдали → последнюю закрыл → «Готово»
    with SessionLocal() as db:
        db.get(WoPart, coming_id).status = "reserved"
        db.get(WorkOrder, oid).status = "instock"
        db.commit()
    login(client, "andrey", "andrey123")
    client.post(f"{url}/issue-request", data={})
    client.post("/logout")
    with SessionLocal() as db:
        req2 = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == oid, IssueRequest.status == "new"))
    login(client, "pavel", "pavel123")
    client.post(f"/stock/issues/{req2.id}/issue")
    client.post("/logout")
    assert _wo(oid).status == "work"
    login(client, "andrey", "andrey123")
    r = client.post(f"{url}/works/{w2_id}/state", data={"action": "done"})   # шаровую выдали, но не подтвердил
    assert _st(w2_id) == "doing" and "Получил" in r.text
    client.post(f"/stock/issues/{req2.id}/received")
    client.post(f"{url}/works/{w2_id}/state", data={"action": "done"})
    client.post("/logout")
    assert _st(w2_id) == "done" and _wo(oid).status == "ready"
