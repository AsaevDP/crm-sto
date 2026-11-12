"""12.11.2026 (брат): поиск по точным данным, ремонт по гарантии, «директора нет — решаю сам» по табелю."""
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id, now
from app.models import AttendanceEvent, AuditLog, ClientAccess, OrderReopen, User, WorkOrder, WoWork

from .conftest import login
from .test_step3 import _create
from .test_step6 import _as, _wo


def _uid(lg):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == lg))


def _closed_long_ago(client, plate, phone):
    """Наряд Ирины выдан 10 дней назад — в её списке его уже нет."""
    _as(client, "irina")
    url = _create(client, plate=plate, phone=phone, name="Клиент Гарантийный").json()["url"]
    wo = _wo(url)
    with SessionLocal() as db:
        o = db.get(WorkOrder, wo.id)
        current_branch_id.set(o.branch_id)
        o.status, o.closed_at = "closed", now() - timedelta(days=10)
        db.commit()
    return url, wo


def _presence(login_, direction):
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == login_))
        current_branch_id.set(u.branch_id)
        db.add(AttendanceEvent(user_id=u.id, direction=direction, at=now(), method="card"))
        db.commit()


def test_exact_search_and_warranty(client):
    url, wo = _closed_long_ago(client, "КТ 1301 КТ", "0501301000")
    assert client.get(url).status_code == 403                         # в списке и по ссылке — нет
    assert "КТ 1301 КТ" not in client.get("/orders?q=Гарантийный").text   # по имени — не находит
    assert "КТ 1301 КТ" not in client.get("/orders?q=1301").text          # по кусочку номера — тоже
    page = client.get("/orders?q=КТ1301КТ").text                       # по полному госномеру — находит
    assert "КТ 1301 КТ" in page
    assert client.get(url).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(ClientAccess).where(ClientAccess.user_id == _uid("irina"), ClientAccess.client_id == wo.client_id,
                                                    ClientAccess.reason.like("Найден поиском%")))
        assert db.scalar(select(AuditLog).where(AuditLog.table == "search"))
    # «Ремонт по гарантии» → решает директор (сам менеджер — нет, даже если директора нет)
    page = client.get(url).text
    assert "Ремонт по гарантии" in page
    client.post(f"{url}/warranty-request", data={"reason": "Снова стучит спереди после замены стоек"})
    with SessionLocal() as db:
        r = db.scalar(select(OrderReopen).where(OrderReopen.work_order_id == wo.id, OrderReopen.status == "pending"))
    assert r.kind == "warranty"
    assert client.post(f"/reopen-requests/{r.id}/self").status_code == 403
    _as(client, "director")
    client.post(f"/reopen-requests/{r.id}", data={"action": "approve"})
    o = _wo(url)
    assert o.warranty == "open" and o.status == "ready"
    _as(client, "irina")
    client.post(f"{url}/works", data={"name": "Протяжка стоек по гарантии", "pricing": "fixed", "rate": "900",
                                      "mechanic_id": str(_uid("andrey"))})
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.is_warranty.is_(True)))
    assert w and w.price == 0 and _wo(url).status == "work"
    r = client.post(f"{url}/close", data={"state": "ok"})
    assert "Выполнены гарантийные работы" in r.text                    # обычная выдача — нельзя
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/works/{w.id}/state", data={"action": "start"})
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    _as(client, "irina")
    assert "Напишите, что произошло" in client.post(f"{url}/warranty-done", data={"result": "", "action": "done"}).text
    client.post(f"{url}/warranty-done", data={"result": "Не дотянули гайку стойки — протянули, проверили", "action": "done"})
    o = _wo(url)
    assert o.status == "closed" and o.warranty == "done" and o.warranty_result.startswith("Не дотянули")
    assert o.closed_at and o.closed_at.date() == o.warranty_prev_closed.date()   # в отчётах — день первой выдачи
    act = client.get(f"{url}/act/handover").text
    assert "Гарантийный ремонт" in act and "(по гарантии)" in act


def test_warranty_not_covered(client):
    url, wo = _closed_long_ago(client, "КТ 1302 КТ", "0501302000")
    client.get("/orders?q=0501302000")
    client.post(f"{url}/warranty-request", data={"reason": "Шумит подшипник"})
    with SessionLocal() as db:
        r = db.scalar(select(OrderReopen).where(OrderReopen.work_order_id == wo.id, OrderReopen.status == "pending"))
    _as(client, "deputy")
    client.post(f"/reopen-requests/{r.id}", data={"action": "approve"})
    _as(client, "irina")
    client.post(f"{url}/warranty-done", data={"result": "Подшипник другой стороны — не меняли, ремонт новым нарядом", "action": "reject"})
    o = _wo(url)
    assert o.status == "closed" and o.warranty == "rejected"


def test_self_decide_only_when_bosses_away(client):
    from app.models import OrderAccess
    _as(client, "irina")
    client.post("/orders/access-request", data={"reason": "Ищу наряд по звонку клиента"})
    with SessionLocal() as db:
        a = db.scalar(select(OrderAccess).where(OrderAccess.user_id == _uid("irina"), OrderAccess.status == "pending"))
    _presence("director", "in")                          # директор в здании — сам нельзя
    assert "решаю сам" not in client.get("/orders").text
    r = client.post(f"/access-requests/{a.id}/self")
    assert "найдите их" in r.text
    _presence("director", "out")                         # ушёл; зам не приходил
    _presence("deputy", "out")
    assert "решаю сам" in client.get("/orders").text
    client.post(f"/access-requests/{a.id}/self")
    with SessionLocal() as db:
        a = db.get(OrderAccess, a.id)
        assert a.status == "approved" and a.self_decided and a.hours == 1
        until = a.until if a.until.tzinfo else a.until.replace(tzinfo=now().tzinfo)
        assert until - now() <= timedelta(hours=1, minutes=1)
    _as(client, "director")
    page = client.get("/access-requests").text
    assert "решил(а) сам(а)" in page
    with SessionLocal() as db:
        from app.models import Notification
        assert db.scalar(select(Notification).where(Notification.user_id == _uid("director"), Notification.kind == "self_decided"))
