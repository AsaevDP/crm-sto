"""23.11.2026 (брат, наряд 25): выдали мастеру что-то под наряд — у мастера 🔴 «Подтвердить получение» у «Нарядов»,
на плашке наряда «Подтвердить получение · имя» (раньше «В работе · …»). Нажал «Получил» — гаснет."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import WorkOrder
from app.services import orders as osvc

from .conftest import login
from .test_receipt import _issued_tool


def _red_receipt(client, wid):
    return [x for x in client.get("/api/notify").json()["badges"].get("/orders", [])
            if x["c"] == "red" and str(wid) in x.get("wo", []) and any("Подтвердить получение" in g["t"] for g in x.get("go", []))]


def test_red_badge_and_label_until_received(client):
    tool, rid, url = _issued_tool(client, "Съёмник (кружок получил)", "КА 2301 КА", "0502301000")
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:   # машина в работе — на плашке должно быть «Подтвердить получение», а не «В работе»
        wo = db.get(WorkOrder, wid)
        current_branch_id.set(wo.branch_id)
        wo.status = "work"
        db.commit()
        assert osvc.stage_label(db, wo, "Демченко").startswith("Подтвердить получение")
    login(client, "andrey", "andrey123")
    assert _red_receipt(client, wid)
    assert "Подтвердить получение" in client.get("/orders?tab=active").text
    client.post(f"/tool-requests/{rid}/received")
    assert not _red_receipt(client, wid)
    with SessionLocal() as db:
        assert osvc.stage_label(db, db.get(WorkOrder, wid), "Демченко").startswith("В работе")
    client.post("/logout")
    login(client, "pavel", "pavel123")   # вернуть в шкаф — база общая
    client.post(f"/tools/{tool.id}/returned")
