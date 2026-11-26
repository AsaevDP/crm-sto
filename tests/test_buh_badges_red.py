"""26.11.2026 (брат): у бухгалтера «проверить» и «выдать» — только его дела → кружок у «Зарплаты сотрудников» красный.
Плитки остаются своего цвета (полоска «Проверить» — оранжевая, «Выдать» — зелёная)."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, User
from app.services import payweek as wsvc

from .conftest import login

FETCH = {"X-Requested-With": "fetch"}


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _pay(c):
    return c.get("/api/notify", headers=FETCH).json()["badges"].get("/payroll/weeks", [])


def test_buh_check_and_pay_red(client):
    prev = wsvc.monday(date.today()) - timedelta(days=7)   # кружок считает прошлую неделю
    with SessionLocal() as db:
        a = db.scalar(select(User).where(User.login == "andrey"))
        s = db.scalar(select(User).where(User.login == "sergey"))
        current_branch_id.set(a.branch_id)
        for u in (a, s):
            db.add(PayrollAccrual(user_id=u.id, branch_id=u.branch_id, amount=Decimal("700"), base_amount=Decimal("700"),
                                  rate=0, kind="percent", note="работа (тест кружков)", on_date=prev + timedelta(days=1)))
        db.commit()
        aid = a.id
    _as(client, "buh")
    chk = [x for x in _pay(client) if x["t"].startswith("проверить")]
    assert chk and all(x["c"] == "red" for x in chk) and "Проверить" in chk[0]["tile"]
    # Проверил → утвердил директор → у бухгалтера красное «выдать»
    client.post(f"/payroll/weeks/{prev}/people/{aid}", data={"status": "checked"})
    _as(client, "admin")
    client.post(f"/payroll/weeks/{prev}/people/{aid}", data={"status": "approved"})
    _as(client, "buh")
    pay = [x for x in _pay(client) if "выдать" in x["t"]]
    assert pay and all(x["c"] == "red" for x in pay) and "Выдать" in pay[0]["tile"]
    # Плитки — своего цвета (полоска мигает цветом плитки, не кружка)
    page = client.get(f"/payroll/weeks?d={prev}").text
    assert 'ptile-t">Проверить' in page and 'ptile-t">Выдать' in page
    # Админу — как было (оранжевое «утвердить»), красных зарплатных дел нет
    _as(client, "admin")
    assert not [x for x in _pay(client) if x["c"] == "red" and ("проверить" in x["t"] or "выдать" in x["t"])]
