"""«Рабочие места» (21.11.2026, брат): 5 видов (подъёмник 20, электрик / моторист / мойка / детейлинг по 5), номер
внутри вида, перестановка номеров, мастер только своей должности, переключатель; «Начал» ставит машину на место
мастера; «Пульт» — сотрудники над местами, места группами, без записи на сегодня."""
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Lift, User, WorkOrder
from app.services import places as psvc

from .conftest import login
from .test_step3 import _create


def _u(lg):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == lg))


def test_forty_places_by_kind(client):
    with SessionLocal() as db:
        rows = db.scalars(select(Lift)).all()
        by = {}
        for l in rows:
            by.setdefault(l.kind, set()).add(l.number)
    assert by["lift"] == set(range(1, 21))
    for k in ("electric", "motor", "wash", "detail"):
        assert by[k] == set(range(1, 6)), k
    login(client, "admin", "admin123")
    page = client.get("/settings/lifts").text
    for t in ("Рабочие места", "Подъёмники", "Электрики", "Мотористы", "Мойка", "Детейлинг"):
        assert t in page, t
    client.post("/logout")


def test_guess_and_clean():
    l = SimpleNamespace(mechanic_id=None, name="Пост 7 · Мойка")
    assert psvc._guess_kind(l, {}) == "wash" and psvc._clean_name(l.name) == ""
    assert psvc._guess_kind(SimpleNamespace(mechanic_id=1, name="Пост 4"), {1: "motorist"}) == "motor"
    assert psvc._clean_name("Пост 2 · Подвеска") == "Подвеска"


def test_swap_number_role_check_and_switch(client):
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        a = db.scalar(select(Lift).where(Lift.kind == "electric", Lift.number == 4))
        b = db.scalar(select(Lift).where(Lift.kind == "electric", Lift.number == 5))
    # номер 5 уже есть — места меняются номерами
    client.post(f"/settings/lifts/{a.id}", data={"kind": "electric", "number": "5", "name": "Свет", "active": "1"})
    with SessionLocal() as db:
        assert db.get(Lift, a.id).number == 5 and db.get(Lift, b.id).number == 4
        assert db.get(Lift, a.id).active and db.get(Lift, a.id).name == "Свет"
    # слесаря на место электрика — нельзя
    andrey = _u("andrey")
    client.post(f"/settings/lifts/{a.id}", data={"kind": "electric", "number": "5", "mechanic_id": str(andrey.id), "active": "1"})
    with SessionLocal() as db:
        assert db.get(Lift, a.id).mechanic_id != andrey.id
    # выключить (переключатель без «Сохранить» шлёт ту же форму)
    client.post(f"/settings/lifts/{a.id}", data={"kind": "electric", "number": "5", "name": "Свет"})
    with SessionLocal() as db:
        assert not db.get(Lift, a.id).active
    client.post("/logout")


def test_start_seats_car_and_pult(client):
    andrey = _u("andrey")
    with SessionLocal() as db:
        place = psvc.place_of(db, andrey.id)
        if not place:   # закрепим Андрея за свободным подъёмником
            place = db.scalar(select(Lift).where(Lift.kind == "lift", Lift.active.is_(True), Lift.mechanic_id.is_(None)))
            current_branch_id.set(place.branch_id)
            place.mechanic_id = andrey.id
            db.commit()
        pid = place.id
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="AA5521KC", phone="0675555221").json()["url"]
    client.post("/logout")
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        current_branch_id.set(db.get(WorkOrder, wid).branch_id)
        wo = db.get(WorkOrder, wid)
        assert wo.lift_id is None
        psvc.seat(db, wo, andrey.id)
        db.commit()
        assert db.get(WorkOrder, wid).lift_id == pid
    login(client, "admin", "admin123")
    pult = client.get("/pult").text
    assert "Рабочие места · занято" in pult and "pst-band" in pult and "Запись на сегодня" not in pult
    assert "Подъёмник" in pult
    client.post("/logout")
