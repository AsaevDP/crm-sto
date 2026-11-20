"""Ячейки ключей (18.11.2026, брат): номер крючка выдаётся сам при приёмке, по кругу; все заняты — «Без ячейки»;
после выдачи / отмены крючок свободен; вручную не меняется; виден в наряде крупно у всех ролей."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Branch, Client, Vehicle, WorkOrder
from app.services import keys as ksvc
from app.services import orders as osvc
from app.services import settings as st

from .conftest import login


def _branch(db, count):
    b = Branch(name="Тест ключей", code="K" + uuid.uuid4().hex[:4])
    db.add(b)
    db.flush()
    current_branch_id.set(b.id)
    st.put(db, b.id, "keys", {"count": count, "last": 0})
    db.flush()
    return b


def _wo(db, b, status="created"):
    c = Client(branch_id=b.id, full_name="Ключ", phone=uuid.uuid4().hex[:10])
    db.add(c)
    db.flush()
    v = Vehicle(branch_id=b.id, client_id=c.id, plate=uuid.uuid4().hex[:8].upper())
    db.add(v)
    db.flush()
    wo = WorkOrder(branch_id=b.id, number="K-" + uuid.uuid4().hex[:10], client_id=c.id, vehicle_id=v.id, status=status)
    db.add(wo)
    db.flush()
    ksvc.assign(db, wo)
    db.flush()
    return wo


def test_round_robin_full_and_release(client):
    with SessionLocal() as db:
        b = _branch(db, 3)
        a, b2, c = _wo(db, b), _wo(db, b), _wo(db, b)
        assert [a.key_slot, b2.key_slot, c.key_slot] == [1, 2, 3]
        d = _wo(db, b)                       # все заняты — машину принимаем, «Без ячейки»
        assert d.key_slot == 0 and ksvc.label(d) == "Без ячейки"
        osvc.set_status(db, b2, "closed")    # выдали — крючок 2 свободен
        db.flush()
        e = _wo(db, b)
        assert e.key_slot == 2               # единственный свободный — занимает сразу
        db.rollback()


def test_next_free_goes_around_not_just_freed(client):
    """Только что освободившийся крючок не занимают сразу — сначала следующие по кругу."""
    with SessionLocal() as db:
        b = _branch(db, 5)
        a, _x = _wo(db, b), _wo(db, b)
        osvc.set_status(db, a, "cancel")     # крючок 1 свободен
        db.flush()
        assert _wo(db, b).key_slot == 3
        assert _wo(db, b).key_slot == 4
        assert _wo(db, b).key_slot == 5
        assert _wo(db, b).key_slot == 1      # круг — снова с начала
        db.rollback()


def test_fill_open_and_reopen(client):
    with SessionLocal() as db:
        b = _branch(db, 10)
        old = _wo(db, b)
        old.key_slot = None                  # наряд из времени до ячеек
        done = _wo(db, b, status="closed")
        done.key_slot = None
        db.flush()
        ksvc.fill_open(db)
        assert old.key_slot and done.key_slot is None   # выданным номер не нужен
        osvc.set_status(db, done, "ready")   # открыли заново — машина снова на станции
        assert done.key_slot and done.key_slot != old.key_slot
        db.rollback()


def test_badge_in_order_and_settings(client):
    from .test_step3 import _create
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 7004 КА", phone="0507004000").json()["url"]
    with SessionLocal() as db:
        wo = db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))
        slot = wo.key_slot
    assert slot and slot > 0
    page = client.get(url).text
    assert "key-badge" in page and f'class="key-badge-n">{slot}<' in page
    assert f"🔑{slot}" in client.get("/orders").text      # цифра на плитке
    assert "Ячейка ключей" in client.get(f"{url}/act/intake").text
    client.post("/logout")
    login(client, "admin", "admin123")
    r = client.get("/settings/keys")
    assert r.status_code == 200 and "Крючков в шкафу" in r.text
    assert client.post("/settings/keys", data={"count": "0"}, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert ksvc.count(db, db.scalar(select(WorkOrder.branch_id).where(WorkOrder.id == wo.id))) == 50
    client.post("/logout")
    # 21.11.2026 (брат): та же таблица у менеджера с «Пульта» — только посмотреть; «Выдать со склада» на пульте нет
    login(client, "irina", "irina123")
    pult = client.get("/pult").text
    assert 'href="/keys"' in pult and "Выдать со склада" not in pult
    kp = client.get("/keys").text
    assert "Ячейки ключей" in kp and "key-cell busy" in kp and 'name="count"' not in kp
    client.post("/logout")
