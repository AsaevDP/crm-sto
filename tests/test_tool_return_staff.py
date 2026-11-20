"""«Мой инструмент» мастера (21.11.2026, брат, наряд 17): общий инструмент под наряд можно сдать прямо здесь —
«✓ Получил» → «Сдаю» → «сдаёт · ждёт кладовщика»; у кладовщика в карточке сотрудника — «Принять» (или «Вернул»)."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ToolRequest, User

from .conftest import login
from .test_step3 import _create
from .test_tool_requests import FETCH, _new_shared_tool


def test_return_from_my_tools(client):
    tool = _new_shared_tool(client, "Съёмник (мой инструмент)")
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 9171 КА", phone="0509171000").json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/tool-requests", data={"tool_id": str(tool.id)})
    client.post("/logout")
    login(client, "pavel", "pavel123")
    item = next(x for x in client.get("/api/notify", headers=FETCH).json()["tools"] if "Съёмник (мой" in x["text"])
    client.post(item["done_url"], headers=FETCH)
    client.post("/logout")
    with SessionLocal() as db:
        me = db.scalar(select(User).where(User.login == "andrey"))
    login(client, "andrey", "andrey123")
    page = client.get(f"/tools/staff/{me.id}").text
    assert "✓ Получил" in page                                    # сначала подтвердить
    with SessionLocal() as db:
        r = db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "issue"))
    client.post(f"/tool-requests/{r.id}/received")
    page = client.get(f"/tools/staff/{me.id}").text
    assert f"/tools/{tool.id}/return-request" in page               # «Сдаю» прямо в «Моём инструменте»
    client.post(f"/tools/{tool.id}/return-request?back=/tools/staff/{me.id}")
    assert "сдаёт · ждёт кладовщика" in client.get(f"/tools/staff/{me.id}").text
    client.post("/logout")
    login(client, "pavel", "pavel123")
    page = client.get(f"/tools/staff/{me.id}").text
    assert "сдаёт · ждёт кладовщика" in page and 'href="/stock/issues?tab=tools">Принять' in page
    assert "При увольнении" in page                                 # подсказка: личный — проверкой при увольнении
    with SessionLocal() as db:   # кладовщик принял в шкаф — запрос закрыт (не мешает другим тестам)
        rr = db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "return"))
    client.post(f"/tool-requests/{rr.id}/done", data={"result": "ok"})
    with SessionLocal() as db:
        assert db.get(ToolRequest, rr.id).status == "done"
    client.post("/logout")
