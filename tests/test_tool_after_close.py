"""Инструмент после закрытия наряда (24.11.2026, брат). Общий инструмент под наряд: наряд открыт — ничего не горит,
закрыт / отменён — мастеру 🔴 и уведомление «сдайте в чистом и исправном виде», «Сдаю» красная; нажал «Сдаю» — 🟠
до «Принял» кладовщика. Руководству (кладовщик, менеджер наряда, директор) — 🟠 «не сдали». Личный — не считается."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Tool, ToolRequest, User, WorkOrder

from .conftest import login
from .test_step3 import _create
from .test_tool_requests import FETCH, _new_shared_tool


def _tools_badges(client):
    return client.get("/api/notify").json()["badges"].get("/tools", [])


def _late(b, color):
    return next((x["n"] for x in b if x["c"] == color and ("сдайте" in x["t"] or "сдаёте" in x["t"]
                                                           or "не сдан" in x["t"])), 0)


def test_tool_after_close(client):
    tool = _new_shared_tool(client, "Съёмник (после закрытия)")
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 2411 КА", phone="0502411000").json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    item = next(x for x in client.get("/api/notify", headers=FETCH).json()["tools"] if "после закрытия" in x["text"])
    client.post(item["done_url"], headers=FETCH)
    client.post("/logout")
    with SessionLocal() as db:
        me = db.scalar(select(User).where(User.login == "andrey"))
        wo_id = uuid.UUID(url.rsplit("/", 1)[1])
        r = db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "issue"))

    # наряд открыт — ничего
    login(client, "andrey", "andrey123")
    client.post(f"/tool-requests/{r.id}/received")
    assert not any("сдайте" in x["t"] for x in _tools_badges(client))
    page = client.get(f"/tools/staff/{me.id}").text
    assert "Наряд закрыт — сдайте инструмент" not in page and "btn-ret-late" not in page
    client.post("/logout")

    # наряд закрыт (здесь — сразу в базе)
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wo_id)
        current_branch_id.set(wo.branch_id)
        wo.status = "closed"
        db.commit()
    login(client, "andrey", "andrey123")
    b = _tools_badges(client)
    red = next(x for x in b if x["c"] == "red" and "сдайте" in x["t"])
    assert red["n"] >= 1
    page = client.get(f"/tools/staff/{me.id}").text
    assert "Наряд закрыт — сдайте инструмент" in page and "в чистом и исправном виде" in page and "btn-ret-late" in page
    before_red = red["n"]
    client.post(f"/tools/{tool.id}/return-request?back=/tools/staff/{me.id}")
    b = _tools_badges(client)
    assert _late(b, "red") == before_red - 1
    assert any(x["c"] == "orange" and "сдаёте" in x["t"] for x in b)          # 🟠 до «Принял»
    assert "ret-wait-late" in client.get(f"/tools/staff/{me.id}").text
    client.post("/logout")

    # менеджер наряда и директор видят «не сдали»; блок в «Инструменте»
    for who in ("irina", "director"):
        login(client, who, who + "123")
        assert any(x["c"] == "orange" and "не сдан" in x["t"] for x in _tools_badges(client)), who
        page = client.get("/tools").text
        assert "Не сдали инструмент после закрытия наряда" in page and "Съёмник (после закрытия)" in page, who
        client.post("/logout")
    # второй менеджер (не его наряд) — не видит
    from app.services import tools as tsvc
    with SessionLocal() as db:
        assert any(x["tool"].id == tool.id for x in tsvc.after_close(db, me.branch_id, manager_id=db.get(WorkOrder, wo_id).manager_id))
        assert not any(x["tool"].id == tool.id for x in tsvc.after_close(db, me.branch_id, manager_id=uuid.uuid4()))

    # кладовщик принял — у всех гаснет
    login(client, "pavel", "pavel123")
    with SessionLocal() as db:
        rr = db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "return",
                                                 ToolRequest.status == "new"))
    client.post(f"/tool-requests/{rr.id}/done", data={"result": "ok"})
    with SessionLocal() as db:
        assert db.get(Tool, tool.id).holder_id is None
    assert "Съёмник (после закрытия)" not in client.get("/tools").text.split("Как показать")[0]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    assert "Наряд закрыт — сдайте инструмент" not in client.get(f"/tools/staff/{me.id}").text
    client.post("/logout")
