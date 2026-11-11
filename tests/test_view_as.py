"""«Глазами сотрудника» (12.11.2026): админ видит CRM как сотрудник и может работать за него — записи идут
от имени сотрудника, а в журнале пометка «через админа». Пароль и PIN так поменять нельзя."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Location, User

from .conftest import login


def _menu(html):
    side = html.split('<nav class="sidebar">')[1].split("</nav>")[0]
    return re.findall(r'href="(/[a-z]+)"', side)


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def test_admin_views_as_mechanic(client):
    login(client, "admin", "admin123")
    assert client.get("/view-as").status_code == 200
    client.post(f"/view-as/{_uid('andrey')}")
    html = client.get("/more").text
    assert "Вы работаете как" in html and "Андрей Демченко" in html
    assert _menu(html) == ["/orders", "/schedule", "/stock", "/timesheet", "/payroll", "/tools"]  # меню слесаря
    assert "--accent: var(--green)" in html                            # его цвет
    assert client.get("/settings").status_code == 403                  # его права
    client.post("/view-as/stop")
    assert "/settings" in _menu(client.get("/more").text)              # снова админ


def test_admin_acts_as_storekeeper(client):
    admin, pavel = _uid("admin"), _uid("pavel")
    login(client, "admin", "admin123")
    client.post(f"/view-as/{pavel}")
    # Действие кладовщика: новый стеллаж — проходит, автор — кладовщик, в журнале «через админа»
    client.post("/stock/locations", data={"rack": "VA", "shelves": "1", "places": "1"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "VA"))
        assert loc is not None and loc.created_by == pavel
        log = db.scalar(select(AuditLog).where(AuditLog.record_id == str(loc.id), AuditLog.action == "create"))
        assert log.created_by == pavel and log.via_admin_id == admin
    # Пароль сотрудника так поменять нельзя
    with SessionLocal() as db:
        before = db.get(User, pavel).password_hash
    r = client.post(f"/settings/users/{pavel}/secret", data={"password": "newpass123"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(User, pavel).password_hash == before
    # Сообщения сотрудника не отмечаются прочитанными, пока админ смотрит его глазами
    from app.db import current_branch_id
    from app.models import Notification
    with SessionLocal() as db:
        current_branch_id.set(db.get(User, pavel).branch_id)
        n = Notification(user_id=pavel, kind="parts_ready", title="Тест: сообщение Павлу")
        db.add(n)
        db.commit()
        nid = n.id
    client.post(f"/api/notify/{nid}/read")
    client.get("/notifications")
    with SessionLocal() as db:
        assert db.get(Notification, nid).read_at is None
    client.post("/view-as/stop")
    # В журнале у админа видно: плитка и пометка
    page = client.get("/audit?via=admin&p=all").text
    assert "👑 Админ за сотрудника" in page and "👑 через админа Стас Админов" in page
    # Без режима — пометки нет
    client.post("/stock/locations", data={"rack": "VB", "shelves": "1", "places": "1"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "VB"))
        log = db.scalar(select(AuditLog).where(AuditLog.record_id == str(loc.id), AuditLog.action == "create"))
        assert log.created_by == admin and log.via_admin_id is None


def test_only_admin_can_view_as(client):
    login(client, "irina", "irina123")
    assert client.get("/view-as").status_code == 403
    client.post(f"/view-as/{_uid('andrey')}")
    assert "Вы работаете как" not in client.get("/more").text
