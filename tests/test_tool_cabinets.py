"""Инструмент (11.11.2026): шкафы и полки, раскладка, запасной набор выдаётся взамен личного."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Tool, ToolCabinet, ToolShelf, User

from .conftest import login


def test_cabinets_shelves_and_spare(client):
    login(client, "admin", "admin123")
    client.post("/tools/cabinets", data={"action": "add", "name": "Шкаф Т (тест)", "shelves": "3", "note": "у ворот"})
    with SessionLocal() as db:
        cab = db.scalar(select(ToolCabinet).where(ToolCabinet.name == "Шкаф Т (тест)"))
        shelves = db.scalars(select(ToolShelf).where(ToolShelf.cabinet_id == cab.id).order_by(ToolShelf.sort)).all()
        assert [s.name for s in shelves] == ["Полка 1", "Полка 2", "Полка 3"]
    # Запасной набор — сразу на полку 2
    r = client.post("/tools", data={"name": "Набор головок запасной (тест)", "category": "spare", "cost": "3000",
                                    "shelf_id": str(shelves[1].id)})
    with SessionLocal() as db:
        t = db.scalar(select(Tool).where(Tool.name == "Набор головок запасной (тест)"))
        assert t.shelf_id == shelves[1].id and t.place == "Шкаф Т (тест) · Полка 2" and t.holder_id is None
    for view in ("staff", "cabinets", "spare", "list"):
        page = client.get(f"/tools?view={view}")
        assert page.status_code == 200
    assert "Набор головок запасной (тест)" in client.get("/tools?view=cabinets").text
    assert "Выдать взамен личного" in client.get("/tools?view=spare").text
    # Полку с инструментом удалить нельзя
    client.post("/tools/cabinets", data={"action": "delete_shelf", "sid": str(shelves[1].id)})
    with SessionLocal() as db:
        assert db.get(ToolShelf, shelves[1].id) is not None
    # Выдать запасной слесарю → становится его личным
    with SessionLocal() as db:
        andrey = db.scalar(select(User).where(User.login == "andrey"))
    client.post(f"/tools/{t.id}", data={"action": "move", "to_user": str(andrey.id), "back": "/tools?view=spare"})
    with SessionLocal() as db:
        t = db.get(Tool, t.id)
        assert t.holder_id == andrey.id and t.category == "personal" and t.work_order_id is None
    # Переименовать шкаф — подпись места у инструмента обновится
    client.post("/tools/cabinets", data={"action": "rename", "cid": str(cab.id), "name": "Шкаф Т2 (тест)"})
    with SessionLocal() as db:
        assert db.get(Tool, t.id).place == "Шкаф Т2 (тест) · Полка 2"
    client.post("/logout")
