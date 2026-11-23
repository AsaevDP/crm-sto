"""23.11.2026 (брат): «Подтвердить получение» у мойщика / детейлера.
- химия выдана в запас (без машины) — 🔴 у «Моих запросов», карточка с «✓ Получил»; под машину — 🔴 у «Нарядов»
  (очередь мойки), кнопка в его наряде мойки;
- инструмент, выданный из карточки (полировальная машинка) без наряда, — 🔴 у «Моих запросов»;
- нажал «Получил» — гаснет; у кладовщика в «Недавно» — «ждёт подтверждения», пока не нажал."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ChemRequest, Tool, ToolRequest, User

from .conftest import login
from .test_ready_issue import _chem_in_stock
from .test_tool_requests import _new_shared_tool


def _red(client, href, word):
    """Сколько в красной цифре дел с этим словом (база в тестах общая — сравниваем «до / после»)."""
    return sum(g["n"] for x in client.get("/api/notify").json()["badges"].get(href, []) if x["c"] == "red"
               for g in x.get("go", []) if word in g["t"])


def test_chem_issued_in_stock_needs_receipt(client):
    cat = _chem_in_stock(client, "Полироль салона (получил)", 3)
    login(client, "moyka", "moyka123")
    client.post("/wash/ask", data={"catalog_id": str(cat.id), "qty": "1"})
    with SessionLocal() as db:
        req = db.scalar(select(ChemRequest).where(ChemRequest.catalog_id == cat.id))
    client.post("/logout")
    login(client, "pavel", "pavel123")
    client.post(f"/chem/{req.id}/issue", data={})
    assert "ждёт подтверждения" in client.get("/stock/issues?tab=chem").text
    client.post("/logout")

    login(client, "moyka", "moyka123")
    before = _red(client, "/my-requests", "выдали химию")
    assert before >= 1
    page = client.get("/my-requests?tab=chem").text
    assert f"/chem/{req.id}/received" in page and "в запас" in page
    client.post(f"/chem/{req.id}/received", data={"back": "/my-requests"})
    with SessionLocal() as db:
        assert db.get(ChemRequest, req.id).received_at is not None
    assert _red(client, "/my-requests", "выдали химию") == before - 1


def test_tool_from_card_needs_receipt(client):
    tool = _new_shared_tool(client, "Полировальная машинка (получил)")
    with SessionLocal() as db:   # личный инструмент мойщика — выдаётся без наряда
        db.get(Tool, tool.id).category = "personal"
        moyka = db.scalar(select(User).where(User.login == "moyka"))
        db.commit()
    login(client, "pavel", "pavel123")
    client.post(f"/tools/{tool.id}", data={"action": "move", "to_user": str(moyka.id)})
    client.post("/logout")
    with SessionLocal() as db:
        rid = db.scalar(select(ToolRequest.id).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "issue"))
    assert rid
    login(client, "moyka", "moyka123")
    before = _red(client, "/my-requests", "выдали инструмент")
    assert before >= 1
    assert f"/tool-requests/{rid}/received" in client.get("/my-requests").text
    client.post(f"/tool-requests/{rid}/received")
    assert _red(client, "/my-requests", "выдали инструмент") == before - 1
