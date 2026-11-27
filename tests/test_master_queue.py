"""27.11.2026 (брат): «Очередь мастеров» — колонки по мастерам, менеджер двигает ↑ ↓ «Первым» «В конец»; прогноз
«≈ готово к …» по «Время на выполнение»; у мастера «Моя очередь» и «Следующая»; режим «Обязательная» — «Начал»
только у первой, другую — «Попросить первой»; «Ближайшее свободное» в «Новой записи»."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import MasterQueueRequest, User, WorkOrder, WoWork
from app.services import master_queue as mq

from .conftest import login
from .test_step3 import _create


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _user(lg):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == lg))


def _order(client, plate, phone, minutes):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone).json()["url"]
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    andrey = _user("andrey")
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        current_branch_id.set(wo.branch_id)
        wo.status, wo.mechanic_id = "work", andrey.id
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wid)):
            w.status = "done"   # работы из приёмки не мешают
        db.add(WoWork(work_order_id=wid, name="Работа очереди", pricing="hours", hours=Decimal("1"), rate=800, qty=1,
                      price=800, mechanic_id=andrey.id, minutes=minutes, branch_id=wo.branch_id))
        db.commit()
    return wid, url


def test_queue_forecast_move_and_strict(client):
    w1, u1 = _order(client, "КА 7101 ВВ", "0507101000", 120)
    w2, u2 = _order(client, "КА 7102 ВВ", "0507102000", 90)
    andrey = _user("andrey")
    with SessionLocal() as db:
        bid = db.get(WorkOrder, w1).branch_id
        data = mq.build(db, bid)
        info = data["orders"][w1]
        assert info["label"].startswith("≈ готово ") and "у " in info["where"]
        col = data["cols"][andrey.id]
        ids = [b.wo.id for b in col["queue"]]
        assert w1 in ids and w2 in ids
    # менеджер: экран очереди, «Первым» для второго наряда
    _as(client, "irina")
    page = client.get("/orders/queue")
    assert page.status_code == 200 and "Очередь мастеров" in page.text and 'class="mq-mv"' in page.text
    client.post(f"/orders/queue/{w2}/move", data={"mech": str(andrey.id), "where": "first"})
    with SessionLocal() as db:
        col = mq.build(db, bid, ms=[db.get(User, andrey.id)])["cols"][andrey.id]
        assert col["queue"][0].wo.id == w2
    assert "ocard-eta" in client.get("/orders").text      # прогноз на плитке
    assert "ov-eta" in client.get(u2).text                 # и в шапке наряда
    # мастер: «Моя очередь», «Следующая», экран очереди без кнопок
    _as(client, "andrey")
    lst = client.get("/orders").text
    assert 'id="myq"' in lst and "mq-next" in lst
    assert 'class="mq-mv"' not in client.get("/orders/queue").text
    assert client.post(f"/orders/queue/{w1}/move", data={"mech": str(andrey.id), "where": "first"}).status_code == 403
    # режим «Обязательная» ставит директор
    _as(client, "director")
    client.post("/orders/queue/mode", data={"mech": str(andrey.id), "strict": "1"})
    with SessionLocal() as db:
        assert mq.strict(db, bid, andrey.id)
        w1_work = db.scalar(select(WoWork).where(WoWork.work_order_id == w1, WoWork.name == "Работа очереди")).id
        w2_work = db.scalar(select(WoWork).where(WoWork.work_order_id == w2, WoWork.name == "Работа очереди")).id
    _as(client, "andrey")
    client.post(f"{u1}/works/{w1_work}/state", data={"action": "start"})     # не первая — нельзя
    with SessionLocal() as db:
        assert db.get(WoWork, w1_work).status == "todo"
    # просит взять первой → менеджер согласен → теперь можно
    client.post(f"/orders/queue/{w1}/ask", data={"reason": "Клиент ждёт на месте"})
    with SessionLocal() as db:
        r = db.scalar(select(MasterQueueRequest).where(MasterQueueRequest.work_order_id == w1, MasterQueueRequest.status == "new"))
        assert r
        rid = r.id
    _as(client, "irina")
    b = client.get("/api/notify", headers={"X-Requested-With": "fetch"}).json()["badges"].get("/orders", [])
    assert any("просит взять машину первой" in g["t"] for x in b for g in x["go"])
    client.post(f"/orders/queue/asks/{rid}", data={"action": "ok"})
    _as(client, "andrey")
    client.post(f"{u1}/works/{w1_work}/state", data={"action": "start"})
    with SessionLocal() as db:
        assert db.get(WoWork, w1_work).status == "doing"
        mq.set_strict(db, bid, andrey.id, False)
        for wid in (w1, w2):
            db.get(WorkOrder, wid).status = "closed"
        db.commit()


def test_nearest_free_in_new_appointment(client):
    _as(client, "irina")
    assert "zp-near-chip" in client.get("/schedule").text
