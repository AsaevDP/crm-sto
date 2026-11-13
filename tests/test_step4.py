"""Шаг 4: диагностика и согласование. Критерий из брифа: в подбор уходят только согласованные пункты."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ClientEvent, Lift, User, WoFinding, WorkOrder, WoWork

from .conftest import login
from .test_step3 import JPEG, _create

FETCH = {"x-requested-with": "fetch"}


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _finding(c, url, text, urgency="soon", photo=False):
    files = [("photos", ("f.jpg", JPEG, "image/jpeg")), ("thumbs", ("t.jpg", JPEG, "image/jpeg"))] if photo else None
    return c.post(f"{url}/findings", data={"text": text, "urgency": urgency}, files=files, headers=FETCH)


def _new_b(c, plate):
    login(c, "irina", "irina123")
    phone = "050" + "".join(ch for ch in plate if ch.isdigit()).rjust(7, "0")  # 050 + цифры номера
    url = _create(c, scenario="B", photos=1, plate=plate, phone=phone).json()["url"]
    c.post("/logout")
    return url


def test_full_diagnosis_and_approval(client):
    url = _new_b(client, "АА 2001 АА")
    assert _wo(url).status == "diag"

    # Слесарь (назначен) вносит 3 проблемы, одну с фото
    login(client, "andrey", "andrey123")
    assert _finding(client, url, "Колодки передние 2 мм", "urgent", photo=True).json()["ok"]
    assert _finding(client, url, "Течь прокладки клапанной крышки", "soon").json()["ok"]
    assert _finding(client, url, "Сайлентблоки рычагов подсохли", "advice").json()["ok"]
    assert client.post(f"{url}/findings/x/approval", data={"value": "yes"}).status_code == 403  # слесарь не согласует
    client.post(f"{url}/diag-done")
    assert _wo(url).status == "approve"
    client.post("/logout")

    # Менеджер: 2 «да», 1 «нет»
    login(client, "irina", "irina123")
    with SessionLocal() as db:
        fs = db.scalars(select(WoFinding).where(WoFinding.work_order_id == _wo(url).id).order_by(WoFinding.created_at)).all()
    # Пока не по всем решение — завершить нельзя
    client.post(f"{url}/findings/{fs[0].id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "approve"
    client.post(f"{url}/findings/{fs[1].id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/findings/{fs[2].id}/approval", data={"value": "no"}, headers=FETCH)
    # 13.11.2026: «Да» — сразу работа; согласовать с клиентом нельзя, пока запчастист не закончил подбор
    with SessionLocal() as db:
        assert len(db.scalars(select(WoWork).where(WoWork.work_order_id == _wo(url).id)).all()) == 2
    assert "не закончил подбор" in client.post(f"{url}/approval-done").text
    assert _wo(url).status == "approve"
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/picking-done")                     # подбор: запчасти не нужны
    assert _wo(url).picked_at is not None and _wo(url).status == "approve"
    client.post("/logout")
    login(client, "irina", "irina123")
    client.post(f"{url}/approval-done")
    wo = _wo(url)
    assert wo.status == "work"                             # запчастей нет — сразу в работу
    with SessionLocal() as db:
        works = [w.name for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id))]
        recs = db.scalars(select(ClientEvent).where(ClientEvent.client_id == wo.client_id,
                                                    ClientEvent.kind == "recommendation")).all()
    assert sorted(works) == sorted(["Колодки передние 2 мм", "Течь прокладки клапанной крышки"])
    assert [r.text for r in recs] == ["Сайлентблоки рычагов подсохли"]
    # Карта клиента: рекомендация и вся история
    page = client.get(f"/clients/{wo.client_id}").text
    assert "Сайлентблоки рычагов подсохли" in page and "Решение клиента" in page and "Приёмка" in page
    client.post("/logout")

    # Запчастист видит только согласованное
    login(client, "oleg", "oleg123")
    page = client.get(url).text
    assert "Колодки передние 2 мм" in page and "Течь прокладки" in page


def test_all_declined_goes_ready(client):
    url = _new_b(client, "АА 2002 АА")
    login(client, "andrey", "andrey123")
    _finding(client, url, "Мелкая царапина на диске", "advice")
    client.post(f"{url}/diag-done")
    client.post("/logout")
    login(client, "irina", "irina123")
    with SessionLocal() as db:
        f = db.scalars(select(WoFinding).where(WoFinding.work_order_id == _wo(url).id)).first()
    client.post(f"{url}/findings/{f.id}/approval", data={"value": "no"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "ready"


def test_other_mechanic_cannot_add_finding(client):
    url = _new_b(client, "АА 2003 АА")
    login(client, "sergey", "sergey123")
    assert _finding(client, url, "Чужая проблема").status_code == 403


def test_lift_default_mechanic_and_settings(client):
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        lift = db.scalar(select(Lift).where(Lift.number == 2))
        sergey = db.scalar(select(User).where(User.login == "sergey"))
    # Закрепляем Сергея за постом 2
    client.post(f"/settings/lifts/{lift.id}", data={"name": "Развал", "mechanic_id": str(sergey.id), "active": "1"})
    assert "Развал" in client.get("/settings/lifts").text
    client.post("/logout")
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="АА 2004 АА", phone="0502004000",
                  mechanic_id="", lift_id=str(lift.id)).json()["url"]
    assert _wo(url).mechanic_id == sergey.id
    assert _wo(url).status == "diag"


def test_intake_act_prints(client):
    login(client, "irina", "irina123")
    url = _create(client, plate="АА 2005 АА", phone="0502005000").json()["url"]
    html = client.get(f"{url}/act/intake").text
    assert "АКТ ПРИЁМА-ПЕРЕДАЧИ" in html and "АА 2005 АА" in html and "½" in html


def test_client_note_and_recommendation_resolve(client):
    login(client, "irina", "irina123")
    url = _create(client, plate="АА 2006 АА", phone="0502006000").json()["url"]
    cid = _wo(url).client_id
    client.post(f"/clients/{cid}/note", data={"text": "Звонить после 18:00"})
    assert "Звонить после 18:00" in client.get(f"/clients/{cid}").text
