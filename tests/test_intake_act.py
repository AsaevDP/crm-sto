"""Акт приёма (09.11.2026): только документы и ценные вещи + заметка, бланк A4 с местом для записей после диагностики."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import WorkOrder

from .test_step3 import _create
from .test_step6 import _as


def test_intake_act_print(client):
    _as(client, "irina")
    url = _create(client, plate="AA4545KK", phone="0675550011", intake_items=["docs", "spare", "valuables"],
                  intake_note="Скол на капоте").json()["url"]
    with SessionLocal() as db:
        wo = db.scalar(select(WorkOrder).where(WorkOrder.id == __import__("uuid").UUID(url.rstrip("/").split("/")[-1])))
    assert wo.intake_items == ["docs", "valuables"]  # «запаска» больше не сохраняется
    form = client.get("/orders/new").text
    assert "Запаска" not in form and "Ценные вещи" in form
    act = client.get(f"{url}/act/intake").text
    assert "После диагностики" in act and "Скол на капоте" in act and "Запаска" not in act
    assert "/photos/" in act  # одно фото с приёмки
