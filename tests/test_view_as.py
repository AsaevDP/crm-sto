"""Админ смотрит CRM глазами сотрудника: видит его меню, но ничего не может изменить."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

from .conftest import login


def _menu(html):
    side = html.split('<nav class="sidebar">')[1].split("</nav>")[0]
    return re.findall(r'href="(/[a-z]+)"', side)


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def test_admin_views_as_mechanic_read_only(client):
    login(client, "admin", "admin123")
    assert client.get("/view-as").status_code == 200
    client.post(f"/view-as/{_uid('andrey')}")
    html = client.get("/more").text
    assert "Вы смотрите глазами" in html and "Андрей Демченко" in html
    assert _menu(html) == ["/orders", "/timesheet", "/payroll"]       # меню слесаря
    assert "--accent: var(--green)" in html                            # его цвет
    assert client.get("/settings").status_code == 403                  # его права
    # Любое изменение запрещено
    r = client.post("/clients/00000000-0000-0000-0000-000000000000/note", data={"text": "x"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/view-as/stop")
    assert "/settings" in _menu(client.get("/more").text)              # снова админ


def test_only_admin_can_view_as(client):
    login(client, "irina", "irina123")
    assert client.get("/view-as").status_code == 403
    client.post(f"/view-as/{_uid('andrey')}")
    assert "Вы смотрите глазами" not in client.get("/more").text
