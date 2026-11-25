"""25.11.2026 (брат): карта клиента — «История визитов» списком нарядов (раскрываются, внутри работы, запчасти,
оплаты и «История наряда»), плашки машин; фото машины — одно из фото её нарядов, копия хранится всегда."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Vehicle, WoPhoto, WorkOrder
from app.services import car_photo
from app.services.orders import PHOTO_DIR

from .conftest import login
from .test_step3 import _create


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def test_history_by_orders_and_car_photo(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=2, mechanic_id="", plate="КА 2511 АВ", phone="0632511001").json()["url"]
    wo = _wo(url)
    page = client.get(f"/clients/{wo.client_id}").text
    assert "История визитов · 1" in page and wo.number in page
    assert f'data-feed="/clients/{wo.client_id}/orders/{wo.id}/feed"' in page
    feed = client.get(f"/clients/{wo.client_id}/orders/{wo.id}/feed")
    assert feed.status_code == 200 and 'class="feed"' in feed.text
    # Чужой наряд через адрес этого клиента — нельзя
    url2 = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2511 АС", phone="0632511002").json()["url"]
    wo2 = _wo(url2)
    assert client.get(f"/clients/{wo.client_id}/orders/{wo2.id}/feed").status_code in (403, 404)

    # Фото машины: выбор из фото наряда
    assert "Выбрать фото машины · 2" in page
    with SessionLocal() as db:
        p = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id)).first()
        pid, vid = p.id, wo.vehicle_id
    r = client.post(f"/vehicles/{vid}/photo", data={"photo_id": str(pid)}, follow_redirects=False)
    assert r.status_code in (302, 303)
    with SessionLocal() as db:
        v = db.get(Vehicle, vid)
        assert v.photo.startswith("cars/") and (PHOTO_DIR / v.photo).exists()
        kept = PHOTO_DIR / v.photo
    assert client.get(f"/vehicles/{vid}/photo?t=1").status_code == 200
    assert f"/vehicles/{vid}/photo?t=1" in client.get(f"/clients/{wo.client_id}").text
    # Фото наряда удалили (через год) — закреплённое осталось
    from app.db import current_branch_id
    with SessionLocal() as db:
        ph = db.get(WoPhoto, pid)
        tok = current_branch_id.set(ph.branch_id)
        db.delete(ph)
        db.commit()
        current_branch_id.reset(tok)
    assert kept.exists() and client.get(f"/vehicles/{vid}/photo").status_code == 200
    # Фото чужой машины не закрепить
    with SessionLocal() as db:
        p2 = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo2.id)).first()
        assert car_photo.pin(db, db.get(Vehicle, vid), p2) == "Это фото не этой машины"
    # Убрать
    client.post(f"/vehicles/{vid}/photo", data={"clear": "1"})
    with SessionLocal() as db:
        assert db.get(Vehicle, vid).photo == ""
    assert not kept.exists()
