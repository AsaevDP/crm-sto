"""Инструмент (10.11.2026): категории; личный числится за слесарем, общий выдаётся временно под наряд и работу."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Tool, User, WoWork

from .test_step3 import _create
from .test_step6 import _as


def _tool(name):
    with SessionLocal() as db:
        return db.scalar(select(Tool).where(Tool.name == name))


def _uid(login):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login))


def test_personal_and_shared_tools(client):
    andrey = _uid("andrey")
    _as(client, "pavel")
    client.post("/tools", data={"name": "Ключ личный (тест кат.)", "cost": "300", "category": "personal", "holder_id": str(andrey)})
    client.post("/tools", data={"name": "Стяжки (тест кат.)", "cost": "1600", "category": "chassis", "holder_id": str(andrey)})
    client.post("/tools", data={"name": "Мультиметр (тест кат.)", "cost": "900", "category": "measure"})
    assert _tool("Ключ личный (тест кат.)").holder_id == andrey          # личный — сразу за слесарем
    st = _tool("Стяжки (тест кат.)")
    assert st.holder_id is None and st.category == "chassis"             # общий — на склад
    assert "Стяжки (тест кат.)" in client.get("/tools?cat=chassis").text
    assert "Ключ личный (тест кат.)" not in client.get("/tools?cat=shared").text

    # Общий без наряда не выдаётся
    mm = _tool("Мультиметр (тест кат.)")
    client.post(f"/tools/{mm.id}", data={"action": "move", "to_user": str(andrey)})
    assert _tool("Мультиметр (тест кат.)").holder_id is None

    # Наряд → выдать стяжки под конкретную работу прямо из наряда
    _as(client, "irina")
    url = _create(client, plate="AA9191TT", phone="0679191919", mechanic_id=str(andrey)).json()["url"]
    wo_id = uuid.UUID(url.rstrip("/").split("/")[-1])
    with SessionLocal() as db:
        work = db.scalar(select(WoWork).where(WoWork.work_order_id == wo_id))
    _as(client, "pavel")
    client.post(f"{url}/tools", data={"tool_id": str(st.id), "user_id": str(andrey), "work_id": str(work.id)})
    st = _tool("Стяжки (тест кат.)")
    assert st.holder_id == andrey and st.work_order_id == wo_id and st.wo_work_id == work.id
    assert "Стяжки (тест кат.)" in client.get(url).text                 # видно в наряде
    # Из карточки инструмента — под весь наряд
    client.post(f"/tools/{mm.id}", data={"action": "move", "to_user": str(andrey), "target": f"wo:{wo_id}"})
    assert _tool("Мультиметр (тест кат.)").work_order_id == wo_id

    page = client.get(f"/tools/staff/{andrey}").text                     # видно в карточке слесаря
    assert "Временно, под наряд · 2" in page and "Ключ личный (тест кат.)" in page

    # Вернул — снова на складе, привязка к наряду снята
    client.post(f"/tools/{st.id}", data={"action": "move", "back": url})
    st = _tool("Стяжки (тест кат.)")
    assert st.holder_id is None and st.work_order_id is None

    # Слесарь не может выдавать
    _as(client, "andrey")
    assert client.post(f"{url}/tools", data={"tool_id": str(st.id), "user_id": str(andrey)}).status_code == 403
