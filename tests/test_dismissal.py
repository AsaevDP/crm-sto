"""Увольнение: не удаляет, закрывает вход, передаёт наряды, хранит историю во вкладке «Архив», можно вернуть."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Role, ToolCheck, ToolCheckLine, User, WorkOrder

from .conftest import login
from .test_step3 import _create


def _as(c, who, pwd=None):
    c.post("/logout")
    login(c, who, pwd or who + "123")


def _user(login_):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == login_))


def test_dismiss_archive_rehire(client):
    _as(client, "admin")
    with SessionLocal() as db:
        mech_role = db.scalar(select(Role).where(Role.code == "mechanic"))
    client.post("/settings/users", data={"full_name": "Тест Уходящий", "login": "leaver", "role_id": str(mech_role.id),
                                         "password": "leaver123"})
    uid = _user("leaver").id
    andrey = _user("andrey").id

    # Наряд, где он слесарь
    _as(client, "irina")
    url = _create(client, plate="AA7070XT", phone="0679990011", mechanic_id=str(uid)).json()["url"]
    wo_id = url.rstrip("/").split("/")[-1]

    # Инструмент на руках → уволить нельзя
    _as(client, "pavel")
    client.post("/tools", data={"name": "Ключ (тест увольнения)", "cost": "300", "holder_id": str(uid)})
    _as(client, "director")
    assert "Сначала — проверка инструмента" in client.get(f"/settings/users/{uid}/dismiss").text
    client.post(f"/settings/users/{uid}/dismiss", data={"reason": "x"})
    assert _user("leaver").active

    # Слесарь не может увольнять
    _as(client, "sergey")
    assert client.get(f"/settings/users/{uid}/dismiss").status_code == 403
    assert client.post(f"/settings/users/{uid}/dismiss", data={}).status_code == 403

    # Проверка при увольнении → инструмент на склад
    _as(client, "pavel")
    client.post(f"/tools/staff/{uid}/checks", data={"kind": "dismissal"})
    with SessionLocal() as db:
        c = db.scalar(select(ToolCheck).where(ToolCheck.user_id == uid))
    with SessionLocal() as db:   # по бланку всё отмечено «исправен»
        marks = {f"r_{l.id}": "ok" for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id))}
    client.post(f"/tools/checks/{c.id}", data={**marks, "action": "finish"})

    # Директор увольняет и передаёт наряд Андрею
    _as(client, "director")
    page = client.get(f"/settings/users/{uid}/dismiss").text
    assert "Незакрытые наряды · 1" in page
    client.post(f"/settings/users/{uid}/dismiss", data={"dismissed_on": "2026-11-01", "reason": "По собственному желанию",
                                                         f"wo_{wo_id}_mechanic_id": str(andrey)})
    u = _user("leaver")
    assert not u.active and str(u.dismissed_on) == "2026-11-01" and u.dismiss_reason == "По собственному желанию"
    with SessionLocal() as db:
        assert db.get(WorkOrder, u_id(wo_id)).mechanic_id == andrey
        assert db.scalar(select(AuditLog).where(AuditLog.table == "users", AuditLog.record_id == str(uid),
                                                AuditLog.action == "update")) is not None

    # Войти не может
    client.post("/logout")
    assert client.post("/login", data={"login": "leaver", "password": "leaver123"}, follow_redirects=False).status_code == 401

    # Пропал из рабочих списков, есть в архиве со всей историей
    _as(client, "director")
    assert "Тест Уходящий" not in client.get("/settings/users").text
    assert "Тест Уходящий" in client.get("/settings/users?tab=archive").text
    arch = client.get(f"/settings/users/{uid}/archive").text
    assert "По собственному желанию" in arch and "Нарядов" in arch
    for tab in ("orders", "works", "time", "pay", "tools"):
        assert client.get(f"/settings/users/{uid}/archive?tab={tab}").status_code == 200
    assert "Сдан" in client.get(f"/settings/users/{uid}/archive?tab=tools").text
    _as(client, "irina")
    assert "Тест Уходящий" not in client.get("/orders/new").text

    # Вернуть на работу
    _as(client, "director")
    client.post(f"/settings/users/{uid}/rehire")
    u = _user("leaver")
    assert u.active and u.dismissed_on is None
    _as(client, "leaver", "leaver123")


def test_cannot_dismiss_self(client):
    _as(client, "admin")
    admin = _user("admin")
    client.post(f"/settings/users/{admin.id}/dismiss", data={"reason": "x"})
    assert _user("admin").active


def u_id(v):
    import uuid
    return uuid.UUID(v)
