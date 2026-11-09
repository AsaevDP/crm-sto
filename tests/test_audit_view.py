"""Журнал (09.11.2026): по сотрудникам, фильтры, карточки «было → стало», печать и Excel."""
import io

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Role, User

from .test_step6 import _as


def test_audit_by_employee_filters_print_excel(client):
    _as(client, "admin")
    with SessionLocal() as db:
        mech = db.scalar(select(Role).where(Role.code == "mechanic"))
        admin = db.scalar(select(User).where(User.login == "admin"))
    client.post("/settings/users", data={"full_name": "Журнал Тестовый", "login": "jtest", "role_id": str(mech.id)})
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == "jtest"))
    client.post(f"/settings/users/{uid}", data={"full_name": "Журнал Тестовый-2", "login": "jtest", "role_id": str(mech.id),
                                                "phone": "0501112233"})

    page = client.get("/audit").text
    assert "Все сотрудники" in page and "Стас Админов" in page and "создал" in page

    mine = client.get(f"/audit?user={admin.id}&section=staff&action=update").text
    assert "Журнал Тестовый-2" in mine and "→" in mine and "Телефон" in mine
    assert "вошёл" in client.get(f"/audit?user={admin.id}&action=login").text  # входы видны у самого сотрудника

    assert "ЖУРНАЛ ДЕЙСТВИЙ" in client.get(f"/audit/print?user={admin.id}").text
    r = client.get("/audit/export.xlsx?period=all")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws["C1"].value == "Сотрудник" and ws.max_row > 1

    _as(client, "sergey")
    assert client.get("/audit").status_code == 403
