"""Наряд 11.11.2026: шаги-кнопки, кто ведёт и кто последний менял, лента действий, передача наряда,
пары «менеджер ↔ запчастист» (подстановка при приёмке)."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, Role, User, WorkOrder
from app.security import hash_secret

from .conftest import login
from .test_step3 import _create


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _second_manager():
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == "mgr2"))
        if u:
            return u.id
        irina = db.scalar(select(User).where(User.login == "irina"))
        role = db.scalar(select(Role).where(Role.code == "manager"))
        from app.db import current_branch_id
        current_branch_id.set(irina.branch_id)
        u = User(full_name="Мария Вторая", login="mgr2", role_id=role.id, branch_id=irina.branch_id,
                 password_hash=hash_secret("mgr2pass"))
        db.add(u)
        db.commit()
        return u.id


def test_steps_team_feed_and_handover(client):
    mgr2 = _second_manager()
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 9001 КА", phone="0509001000").json()["url"]
    page = client.get(url).text
    for anchor in ('href="#intake"', 'href="#problems"', 'href="#parts"', 'href="#works"', 'href="#money"'):
        assert anchor in page, anchor                                 # шаги — ссылки на блоки
    assert 'id="intake"' in page and "ведёт" in page and "Ирина Менеджерова" in page
    assert "Лента действий" in page and "принял машину" in page
    client.post("/logout")

    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Свеча (тест ленты)", "qty": "4", "cost": "100", "markup_pct": "30", "action": "order"})
    client.post("/logout")

    login(client, "irina", "irina123")
    page = client.get(url).text
    assert "Последнее изменение: <b>Олег Запчастин</b>" in page
    assert "добавил запчасть «Свеча (тест ленты)»" in page
    assert "Олег Запчастин ·" in page                               # подпись у позиции
    # Передать наряд другому менеджеру — ему сообщение, в ленте видно
    client.post(f"{url}/assign", data={"manager_id": str(mgr2), "partsman_id": str(_wo(url).partsman_id),
                                       "mechanic_id": str(_wo(url).mechanic_id or "")})
    assert _wo(url).manager_id == mgr2
    with SessionLocal() as db:
        n = db.scalar(select(Notification).where(Notification.user_id == mgr2, Notification.kind == "handover"))
        assert n and "наряд" in n.text
    # 13.11.2026 (брат): у Ирины — 3 дня «Переданные», только посмотреть; изменить — нельзя
    page = client.get(url)
    assert page.status_code == 200 and "только просмотр" in page.text
    assert client.post(f"{url}/works", data={"name": "X", "pricing": "fixed", "rate": "1"}).status_code == 403
    client.post("/logout")
    login(client, "mgr2", "mgr2pass")
    page = client.get(url).text
    assert "Мария Вторая" in page and "передал наряд другому менеджеру" in page


def test_paired_partsman_is_suggested_at_intake(client):
    mgr2 = _second_manager()
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        oleg = db.scalar(select(User).where(User.login == "oleg"))
    page = client.get(f"/settings/users/{oleg.id}").text
    assert "Работает с менеджером" in page
    client.post(f"/settings/users/{oleg.id}", data={"full_name": oleg.full_name, "login": "oleg", "role_id": str(oleg.role_id),
                                                   "works_with_id": str(mgr2)})
    with SessionLocal() as db:
        assert db.get(User, oleg.id).works_with_id == mgr2
    client.post("/logout")
    client.post("/login", data={"login": "mgr2", "password": "mgr2pass"})
    page = client.get("/orders/new").text
    assert f'value="{oleg.id}" selected' in page and "Олег Запчастин · ваш" in page
    client.post("/logout")
    login(client, "irina", "irina123")
    assert f'value="{oleg.id}" selected' not in client.get("/orders/new").text   # у Ирины — не подставляется
    client.post("/logout")
    login(client, "admin", "admin123")                                         # вернуть как было
    client.post(f"/settings/users/{oleg.id}", data={"full_name": oleg.full_name, "login": "oleg", "role_id": str(oleg.role_id)})
