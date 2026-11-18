"""Мойка (18.11.2026, брат): прайс (правят менеджер / директор / зам / админ, мойщик только видит), быстрая приёмка
на мойку (услуги из прайса = работы с ценой по классу машины), живая очередь («↑», ≈ время), мойщик «Взять следующую»,
у мойщика свой короткий экран без ремонтных разделов, дальше обычный наряд: оплата → выдача → зарплата мойщику."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, PayrollAccrual, User, WorkOrder, WoWork
from app.models.wash import WashService
from app.services import wash as wsvc

from .conftest import login

FETCH = {"x-requested-with": "fetch"}


def _svc(name):
    with SessionLocal() as db:
        return db.scalar(select(WashService).where(WashService.name == name))


def _intake(c, plate, phone, services, car_class="suv", **extra):
    data = {"phone": phone, "name": "Тест Мойкин", "plate": plate, "make": "Kia", "model": "Sportage",
            "car_class": car_class, "intake_token": uuid.uuid4().hex, **extra}
    files = [("service_id", (None, str(_svc(n).id))) for n in services]
    return c.post("/wash/new", data=data, files=files or None, headers=FETCH)


def _main(html):
    """Только сама страница, без меню и общих скриптов."""
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def _wo_by_plate(plate):
    from app.services.orders import norm_plate
    with SessionLocal() as db:
        from app.models import Vehicle
        v = db.scalar(select(Vehicle).where(Vehicle.plate == norm_plate(plate)))
        return db.scalar(select(WorkOrder).where(WorkOrder.vehicle_id == v.id).order_by(WorkOrder.created_at.desc()))


def test_price_list_rights(client):
    login(client, "irina", "irina123")
    r = client.get("/wash/prices")
    assert r.status_code == 200 and "Комплексная мойка" in r.text and "Добавить услугу" in r.text
    r = client.post("/wash/prices", data={"name": "Антидождь (тест)", "kind": "wash", "price_car": "300", "minutes": "15"},
                    follow_redirects=False)
    assert r.status_code == 303
    s = _svc("Антидождь (тест)")
    assert s and s.price_car == 300 and s.price_suv == 300 and s.price_bus == 300   # пустые классы = как у легковой
    r = client.post(f"/wash/prices/{s.id}", data={"name": s.name, "kind": "wash", "price_car": "350", "price_suv": "1 400",
                                                  "price_bus": "500", "minutes": "20"}, headers=FETCH)
    assert r.status_code == 200
    s = _svc("Антидождь (тест)")
    assert (s.price_car, s.price_suv, s.price_bus, s.minutes) == (350, 1400, 500, 20)
    with SessionLocal() as db:   # правка цены — в журнале
        assert db.scalar(select(AuditLog.id).where(AuditLog.table == "wash_services", AuditLog.record_id == str(s.id)))
    client.post(f"/wash/prices/{s.id}", data={"toggle": "1"})
    assert not _svc("Антидождь (тест)").active
    client.post("/logout")
    # мойщик видит прайс, но править не может
    login(client, "moyka", "moyka123")
    r = client.get("/wash/prices")
    assert r.status_code == 200 and "Добавить услугу" not in r.text and "<input" not in r.text.split("Прайс мойки", 1)[1]
    r = client.post(f"/wash/prices/{s.id}", data={"name": "x", "price_car": "1", "price_suv": "1", "price_bus": "1"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 403) and _svc("Антидождь (тест)").price_car == 350
    assert client.post("/wash/prices", data={"name": "Мойщик добавил", "price_car": "1"}, follow_redirects=False).status_code != 303 \
        or not _svc("Мойщик добавил")
    client.post("/logout")


def test_quick_intake_queue_and_washer_flow(client):
    login(client, "irina", "irina123")
    # без услуги и класса — не принимаем
    r = client.post("/wash/new", data={"phone": "0671110001", "name": "А", "plate": "АА 0001 ММ", "make": "Kia",
                                       "model": "Rio"}, headers=FETCH)
    assert r.status_code == 400 and "Хотя бы одна услуга" in r.json()["errors"] and "Класс машины" in r.json()["errors"]
    # две машины: кроссовер — комплекс + чернение шин; легковая — экспресс
    assert _intake(client, "АА 7001 ММ", "0671117001", ["Комплексная мойка (кузов + салон)", "Чернение шин"]).json()["ok"]
    assert _intake(client, "АА 7002 ММ", "0671117002", ["Экспресс-мойка (поверхностная)"], car_class="car").json()["ok"]
    a, b = _wo_by_plate("АА 7001 ММ"), _wo_by_plate("АА 7002 ММ")
    assert a.kind == "wash" and a.status == "work" and a.car_class == "suv"
    with SessionLocal() as db:
        ws = db.scalars(select(WoWork).where(WoWork.work_order_id == a.id).order_by(WoWork.created_at)).all()
        assert [(w.name, w.pricing, w.price, w.minutes) for w in ws] == [
            ("Комплексная мойка (кузов + салон)", "fixed", 700, 60), ("Чернение шин", "fixed", 100, 10)]
        brd = wsvc.board(db, a.branch_id)
        q = [c.o.id for c in brd["queue"]]
        assert q.index(a.id) < q.index(b.id)
        ca, cb = next(c for c in brd["queue"] if c.o.id == a.id), next(c for c in brd["queue"] if c.o.id == b.id)
        assert cb.eta_start >= ca.eta_end   # одна дорожка (боксов нет): вторая начнёт после первой
    # менеджер поднимает вторую машину выше
    client.post(f"/wash/{b.id}/up")
    with SessionLocal() as db:
        q = [c.o.id for c in wsvc.board(db, a.branch_id)["queue"]]
        assert q.index(b.id) < q.index(a.id)
    # у менеджера наряд мойки — без проблем и запчастей
    page = client.get(f"/orders/{a.id}").text
    assert "Услуги мойки" in page and 'id="problems"' not in page and 'id="parts"' not in page
    client.post("/logout")

    # мойщик: «Наряды» → очередь мойки, «Взять следующую»
    login(client, "moyka", "moyka123")
    r = client.get("/orders", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wash/queue"
    assert "Взять следующую" in client.get("/wash/queue").text
    # пока никто не взял — мойщик видит машину и кнопку «Взять машину»
    page = _main(client.get(f"/orders/{a.id}").text)
    assert "Взять машину" in page
    for word in ("Проблемы", "Диагностика", "Согласование", "Запчасти", "₴"):
        assert word not in page, word
    while True:   # берём по очереди, пока не дойдём до машины A (база в тестах общая)
        r = client.post("/wash/take", follow_redirects=False)
        assert r.status_code == 303
        if r.headers["location"] == f"/orders/{a.id}":
            break
    moyka = None
    with SessionLocal() as db:
        moyka = db.scalar(select(User).where(User.login == "moyka"))
        wo = db.get(WorkOrder, a.id)
        ws = db.scalars(select(WoWork).where(WoWork.work_order_id == a.id).order_by(WoWork.created_at)).all()
        assert wo.mechanic_id == moyka.id and [w.status for w in ws] == ["doing", "todo"]
        assert all(w.mechanic_id == moyka.id for w in ws)
    for w in ws:
        client.post(f"/orders/{a.id}/works/{w.id}/state", data={"action": "done"})
    with SessionLocal() as db:
        assert db.get(WorkOrder, a.id).status == "ready"
    client.post("/logout")

    # детейлер не может взять машину, которую уже взял мойщик
    login(client, "detail", "detail123")
    with SessionLocal() as db:
        assert a.id not in [c.o.id for c in wsvc.board(db, a.branch_id)["queue"]]
    client.post("/logout")

    # менеджер: оплата → выдача → зарплата мойщику
    login(client, "irina", "irina123")
    r = client.post(f"/orders/{a.id}/payments", data={"amount": "800", "method": "cash", "purpose": "full",
                                                      "token": uuid.uuid4().hex}, follow_redirects=False)
    assert r.status_code == 303
    client.post(f"/orders/{a.id}/close", data={"state": "ok"})
    with SessionLocal() as db:
        assert db.get(WorkOrder, a.id).status == "closed"
        acc = db.scalars(select(PayrollAccrual).where(PayrollAccrual.work_order_id == a.id)).all()
        assert acc and all(x.user_id == moyka.id for x in acc)
    client.post("/logout")


def test_washer_sees_only_own_work_in_repair_order(client):
    """Машина с ремонта: мойка — одна из работ большого наряда. Мойщик видит только свою работу."""
    from .test_step3 import _create
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 7003 КА", phone="0507003000").json()["url"]
    wid = url.rsplit("/", 1)[1]
    with SessionLocal() as db:
        moyka = db.scalar(select(User.id).where(User.login == "moyka"))
    client.post(f"{url}/works", data={"name": "Мойка после ремонта (тест)", "pricing": "fixed", "rate": "300",
                                      "mechanic_id": str(moyka)})
    client.post("/logout")
    login(client, "moyka", "moyka123")
    page = _main(client.get(f"/orders/{wid}").text)
    assert "Мойка после ремонта (тест)" in page and "машина с ремонта" in page
    assert "Согласование" not in page and "Запчасти" not in page and "₴" not in page
    client.post("/logout")
