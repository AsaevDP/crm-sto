"""Шаг 2: вход, роли, меню, права, нумерация, журнал."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, User
from app.services.numbering import format_number

from .conftest import login


def _menu(html: str) -> list[str]:
    side = html.split('<nav class="sidebar">')[1].split("</nav>")[0]
    return re.findall(r'href="(/[a-z]+)"', side)


def test_requires_login(client):
    r = client.get("/orders", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password(client):
    r = client.post("/login", data={"login": "irina", "password": "nope"})
    assert r.status_code == 401 and "Неверный" in r.text


def test_roles_see_different_menus(client):
    menus = {}
    for lg, pw in [("irina", "irina123"), ("oleg", "oleg123"), ("pavel", "pavel123"), ("andrey", "andrey123"), ("admin", "admin123")]:
        login(client, lg, pw)
        menus[lg] = _menu(client.get("/more").text)
        client.post("/logout")
    assert "/pult" in menus["irina"] and "/settings" not in menus["irina"]
    assert "/purchases" in menus["oleg"] and "/money" not in menus["oleg"]
    assert "/stock" in menus["pavel"] and "/pult" not in menus["pavel"]
    assert menus["andrey"] == ["/orders", "/timesheet", "/payroll"]
    assert "/settings" in menus["admin"] and "/audit" in menus["admin"]
    # У запчастиста и кладовщика разделы одни и те же (различаются права внутри), остальные — разные
    assert len({tuple(m) for m in menus.values()}) == 4


def test_role_color_on_page(client):
    login(client, "andrey", "andrey123")
    assert "--accent: var(--green)" in client.get("/orders").text


def test_forbidden_section(client):
    login(client, "andrey", "andrey123")
    assert client.get("/settings").status_code == 403
    assert client.get("/money").status_code == 403


def test_pin_and_card_login(client):
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == "andrey"))
    r = client.post("/login/pin", data={"user_id": str(uid), "pin": "0000"})
    assert r.status_code == 401
    r = client.post("/login/pin", data={"user_id": str(uid), "pin": "1005"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/logout")
    r = client.post("/login/card", data={"uid": "0000100002"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Ирина" in client.get("/more").text


def test_user_permission_override_changes_menu(client):
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == "andrey"))
    # Забираем у слесаря «Свою зарплату», добавляем «Склад»
    assert client.post(f"/settings/users/{uid}/perm", data={"perm": "payroll.view_own", "mode": "deny"},
                       headers={"x-requested-with": "fetch"}).json()["ok"]
    client.post(f"/settings/users/{uid}/perm", data={"perm": "stock.view", "mode": "allow"})
    client.post("/logout")
    login(client, "andrey", "andrey123")
    menu = _menu(client.get("/more").text)
    assert "/payroll" not in menu and "/stock" in menu
    client.post("/logout")
    # Возвращаем как было
    login(client, "admin", "admin123")
    client.post(f"/settings/users/{uid}/perm", data={"perm": "payroll.view_own", "mode": "role"})
    client.post(f"/settings/users/{uid}/perm", data={"perm": "stock.view", "mode": "role"})


def test_admin_cannot_lock_self(client):
    login(client, "admin", "admin123")
    with SessionLocal() as db:
        me = db.scalar(select(User).where(User.login == "admin"))
    client.post(f"/settings/users/{me.id}", data={"full_name": me.full_name, "login": "admin", "role_id": str(me.role_id)})
    with SessionLocal() as db:
        assert db.scalar(select(User.active).where(User.login == "admin")) is True


def test_audit_written(client):
    login(client, "admin", "admin123")
    client.post("/settings/branding", data={"name": "СТО Тест", "accent": "red", "idle_minutes": "60"})
    with SessionLocal() as db:
        actions = {(a.table, a.action) for a in db.scalars(select(AuditLog))}
    assert ("users", "login") in actions
    assert ("settings", "create") in actions or ("settings", "update") in actions
    assert "СТО Тест" in client.get("/audit").text or "Журнал" in client.get("/audit").text


def test_number_format():
    assert format_number("{код}-{год}-{номер:6}", "СТО1", 2026, 123) == "СТО1-2026-000123"
    assert format_number("{код}/{гг}/{номер:4}", "A", 2026, 7) == "A/26/0007"
