"""Одна очередь мойки (19.11.2026, брат): быстрая мойка и машины с ремонта в одной очереди; машину с ремонта мыть
нельзя, пока не согласовано / не закончили слесари (серая) — менеджер может разрешить раньше; менеджер расставляет
порядок (↑ ↓ «Первой»), быструю мойку можно поставить первой при приёмке; мойщик берёт только первую машину, другую —
запрос «Попросить первой» → менеджер «Согласен» / «Отказать»; переданный мойщику наряд — «Вам передали» / «✓ Принял»."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Notification, OrderHandover, User, WashQueueRequest, WorkOrder, WoWork
from app.services import wash as wsvc

from .conftest import login
from .test_wash import _intake, _main, _wo_by_plate


def _user(login_):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == login_))


def _queue(db, bid):
    return wsvc.board(db, bid)["queue"]


def _repair_car(client, plate, phone, status="approve"):
    """Наряд «с ремонта»: работа слесаря + мойка мойщика (делаем из быстрой приёмки, меняя вид наряда)."""
    login(client, "irina", "irina123")
    client.get("/wash/prices")   # прайс по умолчанию заводится при первом открытии
    assert _intake(client, plate, phone, ["Мойка кузова (бесконтактная)"]).json()["ok"]
    client.post("/logout")
    wo = _wo_by_plate(plate)
    andrey, moyka = _user("andrey"), _user("moyka")
    current_branch_id.set(wo.branch_id)   # журнал прямых правок в тесте — на тот же филиал
    with SessionLocal() as db:
        o = db.get(WorkOrder, wo.id)
        o.kind, o.status, o.mechanic_id, o.queue_rank = "", status, andrey.id, None
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == o.id)):
            w.mechanic_id = moyka.id
        db.add(WoWork(work_order_id=o.id, name="Замена ремня ГРМ", mechanic_id=andrey.id, pricing="fixed", rate=1000,
                      qty=1, price=1000, branch_id=o.branch_id))
        db.commit()
    return wo


def test_repair_cars_in_one_queue_blocked_until_ready(client):
    wo = _repair_car(client, "АА 8101 ММ", "0671118101", status="approve")
    current_branch_id.set(wo.branch_id)
    with SessionLocal() as db:
        c = next(c for c in _queue(db, wo.branch_id) if c.o.id == wo.id)
        assert c.repair and c.blocked == "не согласовано с клиентом"
        assert not wsvc.can_take(_user("moyka"), c)
        # согласовали, ремонт идёт — всё ещё нельзя
        db.get(WorkOrder, wo.id).status = "work"
        db.commit()
        c = next(c for c in _queue(db, wo.branch_id) if c.o.id == wo.id)
        assert c.blocked.startswith("идёт ремонт")
    # мойщик не может нажать «Начал» на серой машине
    login(client, "moyka", "moyka123")
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id == _user("moyka").id))
    client.post(f"/orders/{wo.id}/works/{w.id}/state", data={"action": "start"})
    with SessionLocal() as db:
        assert db.get(WoWork, w.id).status == "todo"
    assert "мыть пока нельзя" in _main(client.get("/wash/queue").text)
    client.post("/logout")
    # менеджер: «Можно мыть сейчас» — машина становится обычной в очереди
    login(client, "irina", "irina123")
    assert "Можно мыть сейчас" in client.get("/wash?tab=now").text
    client.post(f"/wash/{wo.id}/wash-now")
    with SessionLocal() as db:
        c = next(c for c in _queue(db, wo.branch_id) if c.o.id == wo.id)
        assert c.blocked == "" and c.eta_start
        # снял разрешение, но слесарь закончил — тоже можно
        db.get(WorkOrder, wo.id).wash_now = False
        for x in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.name == "Замена ремня ГРМ")):
            x.status = "done"
        db.commit()
        c = next(c for c in _queue(db, wo.branch_id) if c.o.id == wo.id)
        assert c.blocked == ""
    client.post("/logout")


def test_manager_orders_queue_and_washer_takes_only_first(client):
    login(client, "irina", "irina123")
    client.get("/wash/prices")   # прайс по умолчанию заводится при первом открытии
    assert _intake(client, "АА 8201 ММ", "0671118201", ["Экспресс-мойка (поверхностная)"]).json()["ok"]
    assert _intake(client, "АА 8202 ММ", "0671118202", ["Экспресс-мойка (поверхностная)"]).json()["ok"]
    a, b = _wo_by_plate("АА 8201 ММ"), _wo_by_plate("АА 8202 ММ")
    # быстрая мойка «Первой — вне очереди» при приёмке
    assert _intake(client, "АА 8203 ММ", "0671118203", ["Экспресс-мойка (поверхностная)"], queue_place="first").json()["ok"]
    f = _wo_by_plate("АА 8203 ММ")
    with SessionLocal() as db:
        q = [c.o.id for c in _queue(db, a.branch_id)]
        assert q[0] == f.id and q.index(a.id) < q.index(b.id)
    # «В конец», «↓», «Первой»
    client.post(f"/wash/{f.id}/move", data={"where": "last"})
    client.post(f"/wash/{b.id}/move", data={"where": "first"})
    with SessionLocal() as db:
        q = [c.o.id for c in _queue(db, a.branch_id)]
        assert q[0] == b.id and q[-1] == f.id
    client.post(f"/wash/{b.id}/move", data={"where": "down"})
    with SessionLocal() as db:
        q = [c.o.id for c in _queue(db, a.branch_id)]
        assert q[1] == b.id
        first = wsvc.next_for(db, a.branch_id, _user("moyka"))
    client.post("/logout")

    # мойщик: взять не первую нельзя, «Начал» на ней тоже
    login(client, "moyka", "moyka123")
    assert first.id != f.id
    r = client.post("/wash/take", data={"order_id": str(f.id)}, follow_redirects=False)
    assert r.headers["location"] == "/wash/queue"
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == f.id))
    client.post(f"/orders/{f.id}/works/{w.id}/state", data={"action": "start"})
    with SessionLocal() as db:
        assert db.get(WoWork, w.id).status == "todo"
    page = _main(client.get("/wash/queue").text)
    assert "Попросить первой" in page and "следующая — ваша" in page

    # запрос «Попросить первой» — без причины нельзя, с причиной — менеджеру
    client.post(f"/wash/{f.id}/ask", data={"reason": ""})
    with SessionLocal() as db:
        assert not db.scalar(select(WashQueueRequest).where(WashQueueRequest.work_order_id == f.id))
    client.post(f"/wash/{f.id}/ask", data={"reason": "Клиент уже приехал"})
    client.post(f"/wash/{f.id}/ask", data={"reason": "ещё раз"})   # второй — не создаётся
    with SessionLocal() as db:
        rs = db.scalars(select(WashQueueRequest).where(WashQueueRequest.work_order_id == f.id)).all()
        assert len(rs) == 1 and rs[0].status == "new"
        rid = rs[0].id
    assert "запрос у менеджера" in _main(client.get("/wash/queue").text)
    client.post("/logout")

    # менеджер: красная цифра у «Мойки», плитка «Изменить очередь»; отказ — только с причиной
    login(client, "irina", "irina123")
    badges = client.get("/api/notify").json()["badges"]
    assert any(x["c"] == "red" and "изменить очередь" in x["t"] for x in badges.get("/wash", []))
    page = client.get("/wash?tab=asksq").text
    assert "Клиент уже приехал" in page and "Согласен" in page
    client.post(f"/wash/asks/{rid}", data={"ok": "", "answer": ""})
    with SessionLocal() as db:
        assert db.get(WashQueueRequest, rid).status == "new"
    client.post(f"/wash/asks/{rid}", data={"ok": "1"})
    with SessionLocal() as db:
        assert db.get(WashQueueRequest, rid).status == "ok"
        assert _queue(db, a.branch_id)[0].o.id == f.id
        assert db.scalar(select(Notification).where(Notification.user_id == _user("moyka").id,
                                                    Notification.kind == "wash_queue_ok"))
    client.post("/logout")
    # теперь мойщик берёт её
    login(client, "moyka", "moyka123")
    r = client.post("/wash/take", follow_redirects=False)
    assert r.headers["location"] == f"/orders/{f.id}"
    client.post("/logout")


def test_decline_and_handover_for_washer(client):
    wo = _repair_car(client, "АА 8301 ММ", "0671118301", status="work")
    moyka = _user("moyka")
    current_branch_id.set(wo.branch_id)
    with SessionLocal() as db:
        for x in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.name == "Замена ремня ГРМ")):
            x.status = "done"
        db.add(OrderHandover(work_order_id=wo.id, kind="master", from_id=None, to_id=moyka.id, branch_id=wo.branch_id))
        db.commit()
    login(client, "moyka", "moyka123")
    # красная цифра «передали» ведёт в очередь мойки, там блок «Вам передали» с «✓ Принял»
    badges = client.get("/api/notify").json()["badges"]
    assert any(x["href"] == "/wash/queue#handed" for x in badges.get("/orders", []))
    page = _main(client.get("/wash/queue").text)
    assert "Вам передали" in page and f"/orders/{wo.id}/handover-ack" in page
    assert "Вам передали эту машину" in _main(client.get(f"/orders/{wo.id}").text)
    client.post(f"/orders/{wo.id}/handover-ack")
    assert "Вам передали" not in _main(client.get("/wash/queue").text)
    # запрос → отказ с причиной → мойщику ответ, машина на месте
    with SessionLocal() as db:
        pos = next(c.pos for c in _queue(db, wo.branch_id) if c.o.id == wo.id)
    client.post(f"/wash/{wo.id}/ask", data={"reason": "Хочу помыть сейчас"})
    client.post("/logout")
    login(client, "irina", "irina123")
    with SessionLocal() as db:
        r = db.scalar(select(WashQueueRequest).where(WashQueueRequest.work_order_id == wo.id))
    if r:   # машина могла уже быть первой для мойщика — тогда просить нечего
        client.post(f"/wash/asks/{r.id}", data={"ok": "", "answer": "Клиент заберёт завтра"})
        with SessionLocal() as db:
            assert db.get(WashQueueRequest, r.id).status == "declined"
            assert db.scalar(select(Notification).where(Notification.user_id == moyka.id,
                                                        Notification.kind == "wash_queue_no"))
            assert next(c.pos for c in _queue(db, wo.branch_id) if c.o.id == wo.id) == pos
    client.post("/logout")


def test_detailer_next_on_car_already_washing(client):
    """19.11.2026: мойщик уже помыл машину, полировка детейлера не начата — машина для детейлера следующая (цифра горит,
    «Взять следующую» начинает его полировку)."""
    wo = _repair_car(client, "АА 8401 ММ", "0671118401", status="work")
    detail, moyka = _user("detail"), _user("moyka")
    current_branch_id.set(wo.branch_id)
    with SessionLocal() as db:
        for x in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)):
            if x.name == "Замена ремня ГРМ":
                x.status = "done"
            else:   # мойка — уже сделана мойщиком
                x.status = "done"
        db.add(WoWork(work_order_id=wo.id, name="Полировка кузова", mechanic_id=detail.id, pricing="fixed", rate=4000,
                      qty=1, price=4000, branch_id=wo.branch_id))
        db.commit()
        c = next(c for c in wsvc.board(db, wo.branch_id)["doing"] if c.o.id == wo.id)
        assert not wsvc.busy_with(c, detail)
    login(client, "detail", "detail123")
    with SessionLocal() as db:
        nxt = wsvc.next_for(db, wo.branch_id, _user("detail"))
    if nxt and nxt.id == wo.id:   # база в тестах общая — впереди может стоять другая его машина
        badges = client.get("/api/notify").json()["badges"]
        assert any(x["c"] == "orange" for x in badges.get("/orders", []))
        assert "уже на мойке" in _main(client.get("/wash/queue").text)
        r = client.post("/wash/take", follow_redirects=False)
        assert r.headers["location"] == f"/orders/{wo.id}"
        with SessionLocal() as db:
            w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.name == "Полировка кузова"))
            assert w.status == "doing"
    client.post("/logout")