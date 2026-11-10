"""Инструмент через запросы (11.11.2026): слесарь просит под наряд → кладовщику окошко «Выдать» → выдал;
слесарь «Сдаю» → кладовщик «Принял» (или «сломан»). Вкладка «Выдача → Инструмент», место в шкафу."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Tool, ToolRequest, User, WorkOrder

from .conftest import login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _new_shared_tool(client, name):
    login(client, "pavel", "pavel123")
    client.post("/tools", data={"name": name, "category": "brakes", "cost": "900", "place": "Шкаф 2 · полка 3"})
    client.post("/logout")
    with SessionLocal() as db:
        return db.scalar(select(Tool).where(Tool.name == name))


def test_mechanic_asks_storekeeper_issues_and_takes_back(client):
    tool = _new_shared_tool(client, "Сепаратор (тест запросов)")
    assert tool.place == "Шкаф 2 · полка 3" and tool.holder_id is None
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 9101 КА", phone="0509101000").json()["url"]
    client.post("/logout")

    # Слесарь просит
    login(client, "andrey", "andrey123")
    page = client.get(url).text
    assert "Попросить инструмент" in page and "Сепаратор (тест запросов)" in page
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    assert "ждёт кладовщика" in client.get(url).text
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})        # повторно — нельзя
    client.post("/logout")
    with SessionLocal() as db:
        reqs = db.scalars(select(ToolRequest).where(ToolRequest.tool_id == tool.id)).all()
    assert len(reqs) == 1 and reqs[0].kind == "issue"

    # Кладовщик: окошко и вкладка «Инструмент», выдаёт кнопкой из окошка
    login(client, "pavel", "pavel123")
    n = client.get("/api/notify", headers=FETCH).json()
    item = next(x for x in n["tools"] if "Сепаратор" in x["text"])
    assert item["kind"] == "issue" and "Шкаф 2 · полка 3" in item["text"] and "Андрей" in item["who"]
    page = client.get("/stock/issues?tab=tools").text
    assert "Просят выдать" in page and "📍 Шкаф 2 · полка 3" in page
    assert client.post(item["done_url"], headers=FETCH).json()["ok"]
    with SessionLocal() as db:
        t = db.get(Tool, tool.id)
        andrey = db.scalar(select(User).where(User.login == "andrey"))
        assert t.holder_id == andrey.id and t.work_order_id == uuid.UUID(url.rsplit("/", 1)[1])
    assert "На руках под наряды" in client.get("/stock/issues?tab=tools").text
    client.post("/logout")

    # Слесарь сдаёт → кладовщик «Принял — сломан»
    login(client, "andrey", "andrey123")
    assert "Сдаю" in client.get(url).text
    client.post(f"/tools/{tool.id}/return-request")
    assert "сдаёт · ждёт кладовщика" in client.get(url).text
    client.post("/logout")
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rid = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id, ToolRequest.status == "new"))
    client.post(f"/tool-requests/{rid}/done", data={"result": "broken", "note": "погнут шток"})
    with SessionLocal() as db:
        t = db.get(Tool, tool.id)
        assert t.holder_id is None and t.status == "broken"
    assert "сломан: погнут шток" in client.get("/stock/issues?tab=tools").text


def test_other_mechanic_cannot_ask_on_foreign_order(client):
    tool = _new_shared_tool(client, "Ключ (чужой наряд)")
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 9102 КА", phone="0509102000").json()["url"]
    client.post("/logout")
    login(client, "sergey", "sergey123")
    r = client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    assert r.status_code in (403, 404) or "Нет доступа" in r.text
    with SessionLocal() as db:
        assert not db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id))
