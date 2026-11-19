"""20.11.2026 (брат): «✓ Получил» у мастера и «Вернул» у кладовщика.

- Инструмент выдан → у мастера 🟢 у «Моих запросов», карточка с «Получил»; после — цифра гаснет.
- Второй способ возврата: принесли к окошку без «Сдаю» → кладовщик в «Выдан под наряды» жмёт «Вернул» → в шкафу.
  Если мастер уже нажал «Сдаю» — тот запрос закрывается; неподтверждённый «Получил» больше не висит.
"""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Tool, ToolRequest, User

from .conftest import login
from .test_step3 import _create
from .test_tool_requests import _new_shared_tool


def _badges(client, href):
    return client.get("/api/notify").json()["badges"].get(href, [])


def _issued_tool(client, name, plate, phone):
    tool = _new_shared_tool(client, name)
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone).json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    client.post("/logout")
    with SessionLocal() as db:
        rid = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id))
    login(client, "pavel", "pavel123")
    client.post(f"/tool-requests/{rid}/done")
    client.post("/logout")
    return tool, rid, url


def _green(client):
    """Сколько в зелёной цифре у «Моих запросов» (база в тестах общая — сравниваем «до / после»)."""
    return sum(x["n"] for x in _badges(client, "/my-requests") if x["c"] == "green")


def test_master_confirms_tool_and_tiles(client):
    tool, rid, url = _issued_tool(client, "Съёмник (получил)", "КА 2001 КА", "0502001000")
    login(client, "andrey", "andrey123")
    assert any(x["c"] == "green" and "подтвердите" in x["t"] for x in _badges(client, "/my-requests"))
    before = _green(client)
    page = client.get("/my-requests?tab=tools").text
    assert "ptile" in page and "подтвердить:" in page and f"/tool-requests/{rid}/received" in page
    assert 'class="segmented iss-tabs"' not in page   # маленький переключатель заменён плитками
    client.post(f"/tool-requests/{rid}/received")
    with SessionLocal() as db:
        assert db.get(ToolRequest, rid).received_at is not None
    assert _green(client) == before - 1
    assert f"/tool-requests/{rid}/received" not in client.get("/my-requests?tab=tools").text
    client.post("/logout")
    login(client, "pavel", "pavel123")   # вернуть в шкаф — база общая
    client.post(f"/tools/{tool.id}/returned")
    client.post("/logout")


def test_keeper_returned_without_sdayu(client):
    tool, rid, url = _issued_tool(client, "Пресс (вернул)", "КА 2002 КА", "0502002000")
    login(client, "andrey", "andrey123")
    before = _green(client)
    client.post("/logout")
    login(client, "pavel", "pavel123")
    page = client.get("/tools?view=list&cat=shared&holder=out").text
    assert "Пресс (вернул)" in page and f"/tools/{tool.id}/returned" in page
    client.post(f"/tools/{tool.id}/returned?back=/tools%3Fview%3Dlist%26cat%3Dshared%26holder%3Dout")
    with SessionLocal() as db:
        t = db.get(Tool, tool.id)
        assert t.holder_id is None and t.work_order_id is None
        assert db.get(ToolRequest, rid).received_at is not None   # «Получил» больше не висит у мастера
    client.post("/logout")
    login(client, "andrey", "andrey123")
    assert _green(client) == before - 1
    client.post("/logout")


def test_keeper_returned_closes_sdayu(client):
    tool, rid, url = _issued_tool(client, "Тиски (сдаю)", "КА 2003 КА", "0502003000")
    login(client, "andrey", "andrey123")
    client.post(f"/tool-requests/{rid}/received")
    client.post(f"/tools/{tool.id}/return-request")
    client.post("/logout")
    login(client, "pavel", "pavel123")
    assert any(x["c"] == "orange" for x in _badges(client, "/tools"))   # сдают — принять
    client.post(f"/tools/{tool.id}/returned")
    with SessionLocal() as db:
        assert db.get(Tool, tool.id).holder_id is None
        assert not db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.status == "new"))
    assert not any("сдают" in x["t"] for x in _badges(client, "/tools"))
    # повторно — уже в шкафу, ошибка без падения
    assert client.post(f"/tools/{tool.id}/returned").status_code == 200
    client.post("/logout")
    # мастеру «Вернул» нельзя
    login(client, "andrey", "andrey123")
    assert client.post(f"/tools/{tool.id}/returned", follow_redirects=False).status_code == 403
    client.post("/logout")
