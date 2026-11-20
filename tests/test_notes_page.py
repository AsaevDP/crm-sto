"""«Сообщения» (20.11.2026, брат): новые сверху, прочитанные серым; уведомление о сделанном деле — «✓ сделано» и в
красную цифру не идёт; плитки-фильтры по группам; «глазами сотрудника» — плашка, прочитанным не отмечается."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import ChemRequest, Notification, User
from app.services import notes

from .conftest import login


def _user(lg):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == lg))


def _notify(user, kind, title, wid=None):
    with SessionLocal() as db:
        current_branch_id.set(user.branch_id)
        db.add(Notification(user_id=user.id, kind=kind, title=title, text="", url="/", work_order_id=wid,
                            branch_id=user.branch_id))
        db.commit()


def test_done_not_counted_and_page_sections(client):
    pavel = _user("pavel")
    with SessionLocal() as db:   # химию никто не просит — уведомление «просят химию» уже сделано
        for r in db.scalars(select(ChemRequest).where(ChemRequest.status == "new")):
            r.status = "declined"
        db.commit()
    _notify(pavel, "chem_ask", "Просят химию — тест")
    _notify(pavel, "msg", "Сообщение — тест")
    login(client, "pavel", "pavel123")
    notes_ = client.get("/api/notify").json()["notes"]
    titles = {n["title"] for n in notes_}
    assert "Сообщение — тест" in titles and "Просят химию — тест" not in titles   # сделанное — не в цифре
    page = client.get("/notifications").text
    assert "Новые ·" in page and "Прочитанные и сделанные ·" in page and "✓ сделано" in page
    assert "Склад и выдача" in page and "Переписка" in page
    f = client.get("/notifications?f=msg").text
    right = f.split("Новые ·")[1]
    assert "Сообщение — тест" in right and "Просят химию — тест" not in right
    # второй раз — уже в «Прочитанных»
    again = client.get("/notifications").text
    assert "Сообщение — тест" in again.split("Прочитанные и сделанные ·")[1]


def test_groups():
    assert notes.group_of("parts_in") == "parts" and notes.group_of("issue_ready") == "stock"
    assert notes.group_of("handover") == "people" and notes.group_of("msg") == "msg"
