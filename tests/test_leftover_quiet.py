"""26.11.2026 (брат): решение по «Лишнему после отказов» (оставить / вернуть / продать…) — не сообщение менеджеру,
не строка в карточке клиента; прежняя заметка не стирается, если новую не вписали."""
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ClientEvent, Notification, PartLeftover

from .conftest import login
from .test_site import _leftover_for_sale


def test_sale_decision_is_quiet_and_keeps_note(client):
    cat, lo_id, wid = _leftover_for_sale(client, "Отбойник тест-тихо")
    with SessionLocal() as db:
        lo = db.get(PartLeftover, lo_id)
        lo.note = "Поставщик не принимает отказ"
        db.commit()
        n_note = db.scalar(select(func.count()).select_from(Notification).where(Notification.kind == "refused_done"))
        n_ev = db.scalar(select(func.count()).select_from(ClientEvent).where(ClientEvent.work_order_id == wid))
    login(client, "pavel", "pavel123")   # кладовщик — деталь на складе
    client.post(f"/stock/leftovers/{lo_id}", data={"action": "sale"})
    with SessionLocal() as db:
        lo = db.get(PartLeftover, lo_id)
        assert lo.status == "sale"
        assert lo.note == "Поставщик не принимает отказ"
        assert db.scalar(select(func.count()).select_from(Notification).where(Notification.kind == "refused_done")) == n_note
        assert db.scalar(select(func.count()).select_from(ClientEvent).where(ClientEvent.work_order_id == wid)) == n_ev
