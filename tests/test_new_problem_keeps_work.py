"""22.11.2026 (брат): во время ремонта нашли новую проблему — машина не стоит.
Мастера продолжают согласованные работы, пока менеджер звонит клиенту по новому. И: без запчастиста наряд не пишет
«Подбор готов», а «Согласовал» без запчастиста — только «запчасти не нужны»."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import WoWork, WorkOrder

from .conftest import agree, login
from .test_step3 import _create


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _works(url):
    with SessionLocal() as db:
        return db.scalars(select(WoWork).where(WoWork.work_order_id == _wo(url).id).order_by(WoWork.created_at)).all()


def test_new_problem_masters_keep_working(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="ВВ 7101 ВВ", phone="0507101000").json()["url"]
    agree(client, url, then="andrey")
    assert _wo(url).status == "work"
    w1, w2 = _works(url)[:2]
    assert client.post(f"{url}/works/{w1.id}/state", data={"action": "start"}).status_code in (200, 303)

    # Слесарь нашёл новое — наряд на согласование, но работы продолжаются
    assert client.post(f"{url}/findings", data={"text": "Течёт сальник", "urgency": "soon"}).json()["ok"]
    assert _wo(url).status == "approve"
    assert client.post(f"{url}/works/{w1.id}/state", data={"action": "pause"}).status_code in (200, 303)
    assert client.post(f"{url}/works/{w1.id}/state", data={"action": "done"}).status_code in (200, 303)
    assert client.post(f"{url}/works/{w2.id}/state", data={"action": "start"}).status_code in (200, 303)
    page = client.get(url).text
    assert "Новая проблема — менеджер пишет работы" in page   # 23.11.2026: круг заново — сначала работы
    client.post("/logout")

    # Менеджер пишет работу к новому — мастер начать её не может, пока не согласовано
    login(client, "irina", "irina123")
    client.post(f"{url}/works", data={"name": "Замена сальника", "pricing": "fixed", "rate": "400", "qty": "1"})
    page = client.get(url).text
    assert "Работы записаны — запчасти не нужны" in page   # старые запчасти не мешают кнопке
    client.post("/logout")
    login(client, "andrey", "andrey123")
    new = next(w for w in _works(url) if w.name == "Замена сальника")
    assert new.approval == "pending"
    assert client.post(f"{url}/works/{new.id}/state", data={"action": "start"}).status_code == 403
    client.post("/logout")

    login(client, "irina", "irina123")
    client.post(f"{url}/works-sent", data={"no_parts": "1"})
    client.post(f"{url}/works/{new.id}/approval", data={"value": "yes"}, headers={"x-requested-with": "fetch"})
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "work"


def test_no_partsman_is_not_picked(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="ВВ 7102 ВВ", phone="0507102000", partsman_id="",
                  mechanic_id="").json()["url"]
    client.post(f"{url}/findings", data={"text": "Стучит подвеска", "urgency": "soon"})
    client.post(f"{url}/to-approve")
    wo = _wo(url)
    assert wo.status == "approve" and wo.partsman_id is None
    client.post(f"{url}/works", data={"name": "Ремонт подвески", "pricing": "fixed", "rate": "900", "qty": "1"})
    page = client.get(url).text
    assert "Запчастист не назначен" in page and "Подбор готов →" not in page
    client.post(f"{url}/works-sent")
    assert _wo(url).works_sent_at is None    # без запчастиста на подбор не отправить
    client.post(f"{url}/works-sent", data={"no_parts": "1"})
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}/approval", data={"value": "yes"}, headers={"x-requested-with": "fetch"})
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "work"
