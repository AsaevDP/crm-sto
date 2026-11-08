"""Шаг 6: работы и цены, оплаты, скидка, предоплата, зарплата, закрытие с актом выдачи."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ClientEvent, IssueRequest, PayrollAccrual, PurchaseLine, User, WoPart, WorkOrder, WoWork

from .conftest import login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _order_to_work(c, plate, phone, part_name):
    """Наряд А: 1 работа, 1 запчасть (закупка 100, наценка 100 %) → закупили, пришло, выдали → «В работе»."""
    _as(c, "irina")
    url = _create(c, scenario="A", photos=1, plate=plate, phone=phone, works="Замена колодок").json()["url"]
    _as(c, "oleg")
    c.post(f"{url}/parts", data={"name": part_name, "qty": "1", "cost": "100", "markup_pct": "100", "action": "order"})
    c.post(f"{url}/picking-done")
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    c.post(f"/purchases/{pid}", data={"action": "ordered"})
    _as(c, "pavel")
    c.post(f"/purchases/{pid}/receive", data={})
    _as(c, "andrey")
    c.post(f"{url}/issue-request", data={})
    _as(c, "pavel")
    with SessionLocal() as db:
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id))
    c.post(f"/stock/issues/{rid}/issue")
    assert _wo(url).status == "work"
    return url


def _works(url):
    with SessionLocal() as db:
        return db.scalars(select(WoWork).where(WoWork.work_order_id == _wo(url).id).order_by(WoWork.created_at)).all()


def test_full_money_cycle(client):
    url = _order_to_work(client, "МН 6001 МН", "0506001000", "Колодки (деньги)")

    # Менеджер: работа по нормо-часам 1.5 × 1000, плюс работа по фиксированной цене 500
    _as(client, "irina")
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "hours", "hours": "1.5", "rate": "1000", "qty": "1"})
    client.post(f"{url}/works", data={"name": "Диагностика ходовой", "pricing": "fixed", "rate": "500", "qty": "1"})
    assert [x.price for x in _works(url)] == [Decimal("1500.00"), Decimal("500.00")]

    # Слесарь: Начал → Пауза → Продолжить → Готово по каждой работе → наряд «Готово»
    _as(client, "andrey")
    for x in _works(url):
        client.post(f"{url}/works/{x.id}/state", data={"action": "start"})
        client.post(f"{url}/works/{x.id}/state", data={"action": "pause", "reason": "Ждём деталь"})
        client.post(f"{url}/works/{x.id}/state", data={"action": "start"})
        client.post(f"{url}/works/{x.id}/state", data={"action": "done"})
    assert _wo(url).status == "ready"

    # Скидка 10 %: работы 2000 + запчасти 200 = 2200, скидка 220, итого 1980
    _as(client, "irina")
    client.post(f"{url}/money", data={"payment_type": "post", "discount_pct": "10"})
    client.post(f"{url}/payments", data={"amount": "1000", "method": "cash", "purpose": "extra"})
    page = client.get(url).text
    assert "1980.00" in page or "1 980.00" in page
    assert _wo(url).status == "ready"  # долг 980 — ещё не оплачено
    # Менеджер не может закрыть с долгом
    client.post(f"{url}/close", data={"state": "ok", "debt_comment": "потом"})
    assert _wo(url).status == "ready"
    client.post(f"{url}/payments", data={"amount": "980", "method": "terminal", "purpose": "extra", "receipt_no": "0042"})
    assert _wo(url).status == "paid"

    # Зарплата: слесарю 30 % от работ после скидки (2000 × 0.9 × 0.3 = 540), запчастисту 7 % от наценки (100 × 0.9 × 0.07 = 6.30)
    with SessionLocal() as db:
        acc = {db.get(User, a.user_id).login: a.amount for a in db.scalars(select(PayrollAccrual).where(PayrollAccrual.work_order_id == _wo(url).id))}
        sums = {}
        for a in db.scalars(select(PayrollAccrual).where(PayrollAccrual.work_order_id == _wo(url).id)):
            sums[db.get(User, a.user_id).login] = sums.get(db.get(User, a.user_id).login, 0) + a.amount
    assert sums == {"andrey": Decimal("540.00"), "oleg": Decimal("6.30")}

    # Акт выдачи: «не полностью исправен» без перечня — нельзя
    client.post(f"{url}/close", data={"state": "partial", "defects": ""})
    assert _wo(url).status == "paid"
    client.post(f"{url}/close", data={"state": "partial", "defects": "Скрип ремня генератора", "advice": ""})
    wo = _wo(url)
    assert wo.status == "closed" and wo.handover_state == "partial"
    with SessionLocal() as db:
        recs = [e.text for e in db.scalars(select(ClientEvent).where(ClientEvent.client_id == wo.client_id, ClientEvent.kind == "recommendation"))]
    assert any("Скрип ремня генератора" in r for r in recs)

    # Заказ-наряд для клиента: без артикулов и закупки
    pr = client.get(f"{url}/print").text
    assert "ЗАКАЗ-НАРЯД" in pr and "Колодки (деньги)" in pr and "закупка" not in pr.lower()
    assert "АКТ ВЫДАЧИ" in client.get(f"{url}/act/handover").text

    # Слесарь видит свою зарплату
    _as(client, "andrey")
    assert "540.00" in client.get("/payroll").text


def test_prepay_blocks_picking(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="МН 6002 МН", phone="0506002000", works="Капремонт").json()["url"]
    client.post(f"{url}/money", data={"payment_type": "prepay", "prepay_amount": "5000", "prepay_for": "Детали двигателя"})
    assert _wo(url).awaiting_prepay
    _as(client, "oleg")
    r = client.post(f"{url}/parts", data={"name": "Поршни", "qty": "4", "cost": "1000", "action": "order"})
    assert r.status_code == 403
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "5000", "method": "card", "purpose": "prepay"})
    assert not _wo(url).awaiting_prepay
    _as(client, "oleg")
    assert client.post(f"{url}/parts", data={"name": "Поршни", "qty": "4", "cost": "1000", "action": "order"},
                       follow_redirects=False).status_code == 303


def test_admin_ignore_discount_and_close_with_debt(client):
    url = _order_to_work(client, "МН 6003 МН", "0506003000", "Колодки (без скидки)")
    _as(client, "admin")
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "fixed", "rate": "1000", "qty": "1"})
    client.post(f"{url}/money", data={"payment_type": "post", "discount_pct": "50", "payroll_ignore_discount": "1"})
    assert _wo(url).payroll_ignore_discount
    _as(client, "andrey")
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    assert _wo(url).status == "ready"
    _as(client, "admin")
    # Главный админ закрывает с долгом — только с комментарием
    client.post(f"{url}/close", data={"state": "ok"})
    assert _wo(url).status == "ready"
    client.post(f"{url}/close", data={"state": "ok", "debt_comment": "Постоянный клиент, доплатит в пятницу"})
    assert _wo(url).status == "closed"
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.work_order_id == _wo(url).id, PayrollAccrual.wo_work_id == w.id))
    assert a.amount == Decimal("300.00")  # 30 % от полной 1000 — скидка не уменьшила


def test_payroll_scheme_per_unit_for_group(client):
    """Особая ставка: фикс за штуку на группу деталей вместо % от наценки."""
    _as(client, "admin")
    client.post("/settings/payroll/groups", data={"name": "Профицит (тест)"})
    with SessionLocal() as db:
        from app.models import PartGroup
        g = db.scalar(select(PartGroup).where(PartGroup.name == "Профицит (тест)"))
        oleg = db.scalar(select(User).where(User.login == "oleg"))
    client.post(f"/settings/payroll/{oleg.id}/lines", data={"kind": "percent", "base": "parts_margin", "rate": "7"})
    client.post(f"/settings/payroll/{oleg.id}/lines", data={"kind": "per_unit", "rate": "50", "filter_group_id": str(g.id)})
    url = _order_to_work(client, "МН 6004 МН", "0506004000", "Фильтр (профицит)")
    with SessionLocal() as db:
        from app.models import PartCatalog
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == "Фильтр (профицит)"))
    _as(client, "admin")
    client.post(f"/settings/payroll/groups/{g.id}/items", data={"catalog_id": str(cat.id)})
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "fixed", "rate": "100", "qty": "1"})
    _as(client, "andrey")
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "300", "method": "cash", "purpose": "full"})
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.work_order_id == _wo(url).id, PayrollAccrual.user_id == oleg.id))
    assert (a.kind, a.amount) == ("per_unit", Decimal("50.00"))
