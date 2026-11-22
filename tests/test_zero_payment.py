"""22.11.2026 (брат, наряд 21): клиент оплатил всё заранее, работы готовы — «Провести оплату 0 ₴» переводит наряд в
«Оплачено». Без оплаты заранее 0 не проходит; 1 ₴ сверх оплаченного — понятное сообщение, а не тупик."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Payment, WoWork, WorkOrder
from app.services import money as msvc
from app.services import orders as osvc

from .conftest import login
from .test_step3 import _create


def _setup(client, plate, phone, pay: bool):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone).json()["url"]
    wid = uuid.UUID(url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        current_branch_id.set(wo.branch_id)
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wid)):
            w.hours, w.status = Decimal("1"), "done"
            w.price = msvc.work_price(w)
        db.flush()
        if pay:
            db.add(Payment(work_order_id=wid, amount=msvc.totals(db, wo).total, method="terminal", purpose="full",
                           receipt_no="6193"))
        osvc.set_status(db, wo, "ready", "тест: готово, оплачено заранее")   # как наряд 21: оплачен, а статус «Готово»
        db.commit()
    return url, wid


def _status(wid):
    with SessionLocal() as db:
        return db.get(WorkOrder, wid).status


def test_zero_after_prepaid(client):
    url, wid = _setup(client, "ВВ 7201 ВВ", "0507201000", pay=True)
    page = client.get(url).text
    assert "проведите <b>0 ₴</b>" in page and "pay-line" in page
    r = client.post(f"{url}/payments", data={"amount": "1", "method": "cash", "purpose": "full"}, follow_redirects=True)
    assert "Клиент уже оплатил всё" in r.text and _status(wid) == "ready"
    client.post(f"{url}/payments", data={"amount": "0", "method": "cash", "purpose": "full"})
    assert _status(wid) == "paid"
    with SessionLocal() as db:
        assert len(db.scalars(select(Payment).where(Payment.work_order_id == wid)).all()) == 1   # нулевого платежа нет


def test_zero_without_prepay_refused(client):
    url, wid = _setup(client, "ВВ 7202 ВВ", "0507202000", pay=False)
    r = client.post(f"{url}/payments", data={"amount": "0", "method": "cash", "purpose": "full"}, follow_redirects=True)
    assert "Сумма должна быть больше нуля" in r.text and _status(wid) == "ready"
