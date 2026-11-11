"""Схемы и оклад (11.11.2026): плитки должностей, внутри — сотрудники; оклад можно добавить, исправить в строке и убрать."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PayScheme, PaySchemeLine, User, UserPayScheme

from .test_step6 import _as


def _lines(login_):
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == login_))
        ids = db.scalars(select(UserPayScheme.scheme_id).where(UserPayScheme.user_id == u.id)).all()
        return u, db.scalars(select(PaySchemeLine).where(PaySchemeLine.scheme_id.in_(ids))).all()


def test_salary_add_edit_delete(client):
    _as(client, "admin")
    page = client.get("/settings/payroll").text
    assert 'href="/settings/payroll?role=mechanic"' in page and "Выберите должность" in page
    page = client.get("/settings/payroll?role=mechanic").text
    assert "Сергей Ключников" in page and "Ирина" not in page   # только слесари

    u, before = _lines("sergey")
    r = client.post(f"/settings/payroll/{u.id}/lines", data={"kind": "salary", "salary_period": "day", "rate": "650"},
                    follow_redirects=False)
    assert r.status_code == 303 and "role=mechanic" in r.headers["location"]
    _, after = _lines("sergey")
    line = next(l for l in after if l.kind == "salary" and l not in before and l.rate == Decimal("650"))
    assert "Оклад 650 ₴" in client.get("/settings/payroll?role=mechanic").text

    # Исправить сумму в строке
    r = client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "720,5", "role": "mechanic"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/payroll?role=mechanic"
    with SessionLocal() as db:
        assert db.get(PaySchemeLine, line.id).rate == Decimal("720.5")
    assert "Оклад 720,5 ₴" in client.get("/settings/payroll?role=mechanic").text

    # Не админ — нельзя
    _as(client, "buh")
    assert client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "1"}).status_code == 403

    # Убрать
    _as(client, "admin")
    client.post(f"/settings/payroll/lines/{line.id}/delete", data={"role": "mechanic"})
    with SessionLocal() as db:
        assert db.get(PaySchemeLine, line.id) is None
