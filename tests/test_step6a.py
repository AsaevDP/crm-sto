"""Шаг 6а: инструмент — выдача, проверка, удержание из зарплаты, блок увольнения."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PayrollAccrual, Tool, ToolCheck, ToolCheckLine, User

from .conftest import login


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _tool(name):
    with SessionLocal() as db:
        return db.scalar(select(Tool).where(Tool.name == name))


def test_tool_lifecycle_check_and_deduction(client):
    sergey = _uid("sergey")
    _as(client, "pavel")  # кладовщик заводит и выдаёт
    for name, cost in (("Набор головок 94 пр. (тест)", "4200"), ("Динамометрический ключ (тест)", "3100"), ("Съёмник (тест)", "850")):
        client.post("/tools", data={"name": name, "serial": "SN-" + cost, "cost": cost, "holder_id": str(sergey)})
    assert _tool("Набор головок 94 пр. (тест)").holder_id == sergey
    assert _tool("Набор головок 94 пр. (тест)").inv_no.startswith("И-")

    # Слесарь видит свой инструмент, но не чужой список
    _as(client, "sergey")
    r = client.get("/tools", follow_redirects=True)
    assert "Мой инструмент" in r.text and "Динамометрический ключ (тест)" in r.text
    assert client.post("/tools", data={"name": "x"}).status_code == 403

    # Плановая проверка: ключ — сломан по вине (удержание = стоимость), съёмник — износ
    _as(client, "pavel")
    client.post(f"/tools/staff/{sergey}/checks", data={"kind": "planned"})
    with SessionLocal() as db:
        c = db.scalar(select(ToolCheck).where(ToolCheck.user_id == sergey).order_by(ToolCheck.created_at.desc()))
        lines = {db.get(Tool, l.tool_id).name: l for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id))}
    key, puller = lines["Динамометрический ключ (тест)"], lines["Съёмник (тест)"]
    # 11.11.2026: в новой проверке ничего не отмечено; бланк печатается пустым; завершить без отметок нельзя
    assert key.result == "" and puller.result == ""
    blank = client.get(f"/tools/checks/{c.id}/print?blank=1").text
    assert "БЛАНК ПРОВЕРКИ" in blank and "Динамометрический ключ (тест)" in blank and "☐" in blank
    client.post(f"/tools/checks/{c.id}", data={f"r_{key.id}": "fault", "action": "finish"})
    with SessionLocal() as db:
        assert db.get(ToolCheck, c.id).status == "draft"
    client.post(f"/tools/checks/{c.id}", data={**{f"r_{l.id}": "ok" for l in lines.values()},
                                                 f"r_{key.id}": "fault", f"c_{key.id}": "Сорвана трещотка",
                                                 f"r_{puller.id}": "worn", "action": "finish"})
    with SessionLocal() as db:
        c2 = db.get(ToolCheck, c.id)
        assert c2.status == "done"  # ждёт решения главного админа
        assert db.get(ToolCheckLine, key.id).charge == Decimal("3100.00")
    assert _tool("Динамометрический ключ (тест)").status == "broken"
    assert _tool("Съёмник (тест)").status == "worn"
    # Кладовщик утвердить удержание не может
    assert client.post(f"/tools/checks/{c.id}/approve", data={"approve": str(key.id)}).status_code == 403

    # Главный админ утверждает → минус в зарплате слесаря
    _as(client, "admin")
    client.post(f"/tools/checks/{c.id}/approve", data={"approve": str(key.id)})
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.user_id == sergey, PayrollAccrual.kind == "deduction",
                                                   PayrollAccrual.note.like("%Динамометрический%")))
    assert a.amount == Decimal("-3100.00") and "Динамометрический ключ" in a.note and a.tool_line_id == key.id
    # Видно бухгалтеру / админу / директору: список удержаний за инструмент
    for who in ("admin", "buh", "director"):
        _as(client, who)
        page = client.get("/tools/charges").text
        assert "Динамометрический ключ (тест)" in page and "3 100,00" in page, who
    _as(client, "sergey")
    assert "Инструмент: Динамометрический ключ" in client.get("/payroll").text
    assert "АКТ ПРОВЕРКИ" in client.get(f"/tools/checks/{c.id}/print").text


def test_dismissal_blocked_until_tools_checked(client):
    _as(client, "admin")
    client.post("/settings/users", data={"full_name": "Тест Увольняемый", "login": "fired1", "role_id": "", "password": "fired123"})
    with SessionLocal() as db:
        from app.models import Role
        mech_role = db.scalar(select(Role).where(Role.code == "mechanic"))
    client.post("/settings/users", data={"full_name": "Тест Увольняемый", "login": "fired2", "role_id": str(mech_role.id), "password": "fired123"})
    uid = _uid("fired2")
    client.post("/tools", data={"name": "Тестер (увольнение)", "cost": "500", "holder_id": str(uid)})
    # Уволить нельзя — числится инструмент
    client.post(f"/settings/users/{uid}/dismiss", data={"reason": "тест"})
    with SessionLocal() as db:
        assert db.get(User, uid).active
    # Проверка при увольнении: всё исправно → инструмент на склад, теперь можно отключить
    client.post(f"/tools/staff/{uid}/checks", data={"kind": "dismissal"})
    with SessionLocal() as db:
        c = db.scalar(select(ToolCheck).where(ToolCheck.user_id == uid))
    with SessionLocal() as db:   # по бланку всё отмечено «исправен»
        marks = {f"r_{l.id}": "ok" for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id))}
    client.post(f"/tools/checks/{c.id}", data={**marks, "action": "finish"})
    assert _tool("Тестер (увольнение)").holder_id is None
    with SessionLocal() as db:
        assert db.get(ToolCheck, c.id).status == "approved"  # удерживать нечего
    client.post(f"/settings/users/{uid}/dismiss", data={"reason": "тест"})
    with SessionLocal() as db:
        assert not db.get(User, uid).active
