"""25.11.2026 (брат): поиск и одна карточка клиента.
- Телефон в любом виде (+380 63…, 063…, 63…) находит клиента — во всех поисках, по всем номерам карточки.
- Номер наряда коротко: «39» / «000039»; с галочкой «Год» — этого года, прошлого — «25-39».
- Номер клиента «К-00001»: выдаётся сам, по нему ищут.
- Приёмка: совпал телефон / госномер / VIN — та же карточка; машина у другой карточки — «тот же клиент» (по умолчанию,
  карточки объединяются, номер добавляется) или «новый владелец».
- «Объединить» в карточке — у менеджера."""
import re
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Client, ClientPhone, Vehicle, WorkOrder
from app.services import clients_card as cc
from app.services import numbering

from .conftest import login
from .test_step3 import _create


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def test_phone_forms_and_numbers(client):
    assert cc.norm_phone("+38 (063) 123-45-67") == "380631234567"
    assert cc.norm_phone("063 123 45 67") == "380631234567"
    assert cc.norm_phone("631234567") == "380631234567"
    assert cc.phone_key("+380 63 12") == "6312" and cc.phone_key("063 12") == "6312"

    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2611 КА", phone="0632611001").json()["url"]
    wo = _wo(url)
    with SessionLocal() as db:
        c = db.get(Client, wo.client_id)
        assert c.number and c.code.startswith("К-")
        assert db.scalar(select(ClientPhone).where(ClientPhone.client_id == c.id, ClientPhone.phone == "380632611001"))
        code, seq = c.code, numbering.parts_of(wo.number)["seq"]
    # Клиенты — телефоном в любом виде и номером клиента
    for q in ("+380 63 261 10 01", "063 261 10 01", "632611001", code, code.replace("-", "").lower()):
        items = client.get("/api/clients/search", params={"q": q}).json()["items"]
        assert any(i["id"] == str(c.id) for i in items), q
    # Наряды — коротким номером (этот год), телефоном, номером клиента
    for q in (str(seq), str(seq).zfill(6), "+380632611001", code):
        page = client.get("/orders", params={"q": q, "tab": "all"}).text
        assert wo.number in page, q
    y = wo.created_at.year % 100
    assert wo.number in client.get("/orders", params={"q": f"{y}-{seq}", "tab": "all"}).text
    assert wo.number not in client.get("/orders", params={"q": f"{(y - 1) % 100}-{seq}", "tab": "all"}).text
    # Точный поиск менеджера (доступ к чужому клиенту) — тоже коротко и любым видом телефона
    from app.services import decisions as dsvc
    with SessionLocal() as db:
        assert c.id in dsvc.exact_clients(db, c.branch_id, "063 261 10 01")
        assert c.id in dsvc.exact_clients(db, c.branch_id, code)
    # Настройки: галочки поиска и подсказка
    client.post("/logout")
    login(client, "admin", "admin123")
    page = client.get("/settings/numbering").text
    assert "Поиск по номеру наряда" in page and 'name="search_year"' in page


def test_intake_same_car_other_phone_and_merge(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2612 КА", phone="0632612001").json()["url"]
    c1 = _wo(url).client_id
    # Та же машина, другой телефон: спрашиваем
    j = client.get("/api/intake/owner", params={"phone": "0632612999", "plate": "КА 2612 КА"}).json()
    assert j["conflict"] and "К-" in j["owner"]
    # По умолчанию «тот же клиент» — новая карточка не заводится, номер добавляется
    with SessionLocal() as db:
        n_before = db.scalar(select(Client.id).where(Client.id == c1)) and len(db.scalars(select(Client)).all())
    url2 = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2612 КА", phone="0632612999").json().get("url")
    if url2:
        assert _wo(url2).client_id == c1
    with SessionLocal() as db:
        assert len(db.scalars(select(Client)).all()) == n_before
        assert db.scalar(select(ClientPhone).where(ClientPhone.client_id == c1, ClientPhone.phone == "380632612999"))
    page = client.get(f"/clients/{c1}").text
    assert "Телефоны · 2" in page and "＋ Добавить номер" in page

    # Две карточки одного человека — «Объединить» (менеджер)
    url3 = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2613 КА", phone="0632613001").json()["url"]
    c3 = _wo(url3).client_id
    with SessionLocal() as db:
        code3 = db.get(Client, c3).code
    page = client.get(f"/clients/{c1}", params={"mq": code3}).text
    assert f"/clients/{c1}/merge/{c3}" in page
    client.post(f"/clients/{c1}/merge/{c3}")
    with SessionLocal() as db:
        assert db.get(Client, c3).merged_into == c1
        assert db.scalar(select(Vehicle).where(Vehicle.plate == "КА2613КА")).client_id == c1
        assert db.get(WorkOrder, _wo(url3).id).client_id == c1
    assert client.get(f"/clients/{c3}", follow_redirects=False).headers["location"].endswith(str(c1))
    assert re.search(r"Телефоны · 3", client.get(f"/clients/{c1}").text)
