"""Передача наряда и несколько мастеров (13.11.2026, брат).
«Передать» — менеджеру, мастеру (пост = мастер), запчастисту; новому — «Мне передали» и «✓ Принял», красная цифра.
Прежний менеджер 3 дня видит в «Переданных» — только посмотреть; вернуть может только новый.
Ещё мастер на машине — свой раздел проблем и своя «Диагностика готова»; на согласование — когда готовы все."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Lift, OrderHandover, User, WoFinding, WorkOrder, WoWork

from .conftest import login
from .test_order_team import _second_manager
from .test_step3 import _create
from .test_step6 import _as, _wo

FETCH = {"x-requested-with": "fetch"}


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _badge_handed(client):
    b = client.get("/api/notify", headers=FETCH).json()["badges"].get("/orders", [])
    return sum(x["n"] for x in b if "передали" in x["t"])


def test_handover_master_by_post_and_manager_readonly(client):
    mgr2 = _second_manager()
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="ПД 1301 ПД", phone="0508131000", works="Замена ремня").json()["url"]
    wo = _wo(url)
    assert wo.mechanic_id == _uid("andrey")
    assert "Передать" in client.get(url).text
    # Пост = мастер: переставили на пост Сергея — машина его, несделанная работа — тоже
    with SessionLocal() as db:
        lift = db.scalar(select(Lift).where(Lift.mechanic_id == _uid("sergey")))
    client.post(f"{url}/assign", data={"partsman_id": str(wo.partsman_id), "mechanic_id": str(wo.mechanic_id),
                                       "lift_id": str(lift.id), "manager_id": str(wo.manager_id)})
    o = _wo(url)
    assert o.mechanic_id == _uid("sergey") and o.lift_id == lift.id
    with SessionLocal() as db:
        assert all(w.mechanic_id == _uid("sergey") for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)))
        h = db.scalar(select(OrderHandover).where(OrderHandover.work_order_id == wo.id, OrderHandover.kind == "mechanic"))
        assert h and h.from_id == _uid("andrey") and h.to_id == _uid("sergey") and h.accepted_at is None
    # Андрей машину больше не видит; Сергею — красная цифра, плитка «Мне передали», «✓ Принял»
    _as(client, "andrey")
    assert client.get(url).status_code == 403
    _as(client, "sergey")
    assert _badge_handed(client) >= 1
    assert wo.number in client.get("/orders?tab=handed").text and "Вам передали этот наряд" in client.get(url).text
    client.post(f"{url}/handover-ack")
    with SessionLocal() as db:
        assert db.get(OrderHandover, h.id).accepted_at is not None
    assert wo.number not in client.get("/orders?tab=handed").text

    # Менеджер передал другому → у себя 3 дня «Переданные», только посмотреть; менять — нельзя
    _as(client, "irina")
    o = _wo(url)
    r = client.post(f"{url}/assign", data={"partsman_id": str(o.partsman_id), "mechanic_id": str(o.mechanic_id),
                                           "lift_id": str(o.lift_id), "manager_id": str(mgr2)}, follow_redirects=False)
    assert r.headers["location"] == "/orders?tab=handed_out"
    assert wo.number in client.get("/orders?tab=handed_out").text
    page = client.get(url)
    assert page.status_code == 200 and "только просмотр" in page.text
    assert client.post(f"{url}/works", data={"name": "Попытка", "pricing": "fixed", "rate": "1"}).status_code == 403
    assert client.post(f"{url}/assign", data={"manager_id": str(_uid("irina"))}).status_code == 403   # вернуть сам не может
    # Новый менеджер возвращает — снова можно работать
    client.post("/logout")
    login(client, "mgr2", "mgr2pass")
    assert _badge_handed(client) >= 1
    o = _wo(url)
    client.post(f"{url}/assign", data={"partsman_id": str(o.partsman_id), "mechanic_id": str(o.mechanic_id),
                                       "lift_id": str(o.lift_id), "manager_id": str(_uid("irina"))})
    _as(client, "irina")
    assert "только просмотр" not in client.get(url).text
    client.post(f"{url}/handover-ack")
    assert client.post(f"{url}/works", data={"name": "Проверка ремня", "pricing": "fixed", "rate": "100"},
                       follow_redirects=False).status_code == 303


def test_two_masters_each_own_diagnosis(client):
    _as(client, "irina")
    elektro = _uid("elektro")
    r = client.post("/orders/new", data={"phone": "0508132000", "name": "Два Мастера", "plate": "ПД 1302 ПД", "make": "Kia",
                                         "model": "Sportage", "year": "2017", "mileage": "150000", "scenario": "B",
                                         "complaint": "Не заводится, стучит", "partsman_id": str(_uid("oleg")),
                                         "mechanic_id": str(_uid("andrey")), "lift_id": "", "fuel_level": "50",
                                         "extra_master_id": str(elektro), "extra_work": "Диагностика электрики"},
                    files=[("photos", ("p.jpg", __import__("tests.test_step3", fromlist=["JPEG"]).JPEG, "image/jpeg"))],
                    headers=FETCH)
    url = r.json()["url"]
    assert _wo(url).status == "diag"
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == _wo(url).id, WoWork.mechanic_id == elektro))
        assert w and w.name == "Диагностика электрики"
    # Каждый пишет свои проблемы
    _as(client, "andrey")
    client.post(f"{url}/findings", data={"text": "Стук передней подвески", "urgency": "soon"}, headers=FETCH)
    client.post(f"{url}/diag-done")
    assert _wo(url).status == "diag"                         # электрик ещё не закончил
    _as(client, "elektro")
    page = client.get(url).text
    assert "Стук передней подвески" in page and "Моя диагностика готова" in page and "идёт диагностика" in page
    assert client.post(f"{url}/findings", data={"text": "Окисление клемм АКБ", "urgency": "urgent"}, headers=FETCH).json()["ok"]
    client.post(f"{url}/diag-done")
    assert _wo(url).status == "approve"                      # готовы все — менеджеру на согласование
    _as(client, "irina")
    page = client.get(url).text
    assert "Андрей Демченко" in page and "Окисление клемм АКБ" in page and "Работают с машиной" in page
    with SessionLocal() as db:
        authors = {f.created_by for f in db.scalars(select(WoFinding).where(WoFinding.work_order_id == _wo(url).id))}
    assert authors == {_uid("andrey"), elektro}
    # «+ Добавить мастера» в наряде — моторист, ему «Мне передали»
    client.post(f"{url}/masters", data={"user_id": str(_uid("motor")), "work": "Компрессия"})
    _as(client, "motor")
    assert client.get(url).status_code == 200 and _badge_handed(client) >= 1
