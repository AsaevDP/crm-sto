"""Шаг 3: приёмка. Критерий из брифа: менеджер создаёт наряд с 5 фото, запчастист видит его у себя."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Lift, User, WoPhoto, WorkOrder
from app.services.orders import fmt_plate, norm_phone, norm_plate

from .conftest import login

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200  # минимальный «JPEG» по сигнатуре


def _ids():
    with SessionLocal() as db:
        u = {x.login: str(x.id) for x in db.scalars(select(User))}
        lifts = {l.number: str(l.id) for l in db.scalars(select(Lift))}
    return u, lifts


def _create(c, scenario="A", photos=5, plate="AA1234BC", phone="0671234567", **extra):
    u, lifts = _ids()
    data = {"phone": phone, "name": "Олег Коваленко", "plate": plate, "make": "Toyota", "model": "Camry",
            "year": "2018", "mileage": "125 000", "scenario": scenario, "complaint": "Скрипят тормоза",
            "works": "Замена передних колодок\nЗамена масла", "partsman_id": u["oleg"], "mechanic_id": u["andrey"],
            "lift_id": lifts[4], "fuel_level": "50", "intake_items": ["docs", "spare"], **extra}
    files = [("photos", (f"p{i}.jpg", JPEG, "image/jpeg")) for i in range(photos)]
    files += [("thumbs", (f"t{i}.jpg", JPEG, "image/jpeg")) for i in range(photos)]
    return c.post("/orders/new", data=data, files=files or None, headers={"x-requested-with": "fetch"})


def test_normalize():
    assert norm_plate("aa 1234 bc") == norm_plate("АА1234ВС") == "АА1234ВС"
    assert fmt_plate("АА1234ВС") == "АА 1234 ВС"
    assert norm_phone("067 123-45-67") == norm_phone("+38 (067) 1234567") == "380671234567"


def test_intake_validation(client):
    login(client, "irina", "irina123")
    r = _create(client, photos=0, phone="", plate="")
    assert r.status_code == 400
    errs = " ".join(r.json()["errors"])
    assert "Телефон" in errs and "Госномер" in errs and "фото" in errs


def test_mechanic_cannot_create(client):
    login(client, "andrey", "andrey123")
    assert client.get("/orders/new").status_code == 403


def test_create_with_5_photos_partsman_sees(client):
    login(client, "irina", "irina123")
    r = _create(client)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    with SessionLocal() as db:
        wo = db.get(WorkOrder, __import__("uuid").UUID(url.rsplit("/", 1)[1]))
        assert re.fullmatch(r"СТО1-\d{4}-\d{6}", wo.number)
        assert wo.status == "picking"  # сценарий А сразу в подбор
        assert db.scalar(select(WoPhoto).where(WoPhoto.work_order_id == wo.id).limit(1)) is not None
        n = len(db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id)).all())
        number = wo.number
    assert n == 5
    page = client.get(url).text
    assert "Toyota Camry" in page and "АА 1234 ВС" in page and "Замена передних колодок" in page
    client.post("/logout")

    # Запчастист видит наряд у себя, фото открываются
    login(client, "oleg", "oleg123")
    assert number in client.get("/orders").text
    assert client.get(url).status_code == 200
    with SessionLocal() as db:
        pid = db.scalars(select(WoPhoto.id).where(WoPhoto.work_order_id == wo.id)).first()
    assert client.get(f"/photos/{pid}").status_code == 200
    client.post("/logout")

    # Другой слесарь (не назначен) — не видит ни наряд, ни фото
    login(client, "sergey", "sergey123")
    assert number not in client.get("/orders").text
    assert client.get(url).status_code == 403
    assert client.get(f"/photos/{pid}").status_code == 403


def test_numbers_increment_and_client_reused(client):
    login(client, "irina", "irina123")
    n1 = client.get(_create(client, scenario="B", photos=1, plate="ВС 7777 АА", phone="0991112233").json()["url"]).text
    n2 = client.get(_create(client, scenario="B", photos=1, plate="BC7777AA", phone="099 111 22 33").json()["url"]).text
    a = int(re.search(r"СТО1-\d{4}-(\d{6})", n1).group(1))
    b = int(re.search(r"СТО1-\d{4}-(\d{6})", n2).group(1))
    assert b == a + 1
    # Один клиент и одно авто, хотя номер вводили по-разному
    r = client.get("/api/clients/search?q=7777").json()["items"]
    assert len(r) == 1 and len(r[0]["vehicles"]) == 1
    # Поиск по 4 цифрам номера в списке нарядов
    assert "ВС 7777 АА" in client.get("/orders?q=7777").text


def test_pult_shows_busy_lift(client):
    login(client, "irina", "irina123")
    _create(client, plate="КА 5555 КА", phone="0501234567")
    html = client.get("/pult").text
    assert "КА 5555 КА" in html


def test_cancel_requires_reason(client):
    login(client, "irina", "irina123")
    url = _create(client, plate="АІ 1111 АІ", phone="0631231231").json()["url"]
    client.post(f"{url}/cancel", data={"reason": ""})
    assert "Отменён" not in client.get(url).text.split("История")[0]
    client.post(f"{url}/cancel", data={"reason": "Клиент передумал"})
    assert "Клиент передумал" in client.get(url).text


def test_audit_in_russian(client):
    login(client, "admin", "admin123")
    html = client.get("/audit").text
    assert "Госномер" in html or "Номер" in html
