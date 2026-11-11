"""Недельная зарплата (11.11.2026): неделя → проверил бухгалтер → утвердил админ → выдача в понедельник.
Править суммы, премии и штрафы — только payroll.edit (админ или кому он дал право)."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, PayrollPayout, PayWeek, User, UserPermission
from app.services import payweek as wsvc

from .conftest import login

WEEK = date(2031, 3, 3)   # понедельник далеко в будущем — не пересекается с другими тестами


def _week():
    with SessionLocal() as db:
        return db.scalar(select(PayWeek).where(PayWeek.week_start == WEEK))


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.login == login_))


def test_week_flow(client):
    andrey, sergey, buh = _uid("andrey"), _uid("sergey"), _uid("buh")
    assert buh is not None and buh.role.code == "accountant"
    with SessionLocal() as db:
        current_branch_id.set(andrey.branch_id)
        for u, amount, day in ((andrey, "700.00", 4), (andrey, "300.00", 5), (sergey, "500.00", 6)):
            db.add(PayrollAccrual(user_id=u.id, branch_id=u.branch_id, amount=Decimal(amount), base_amount=Decimal(amount),
                                  rate=0, kind="percent", note="работа (тест недели)", on_date=WEEK.replace(day=day)))
        db.commit()
    assert wsvc.monday(date(2031, 3, 8)) == WEEK and wsvc.pay_day(WEEK) == date(2031, 3, 10)

    # Бухгалтер: видит неделю, отмечает «проверено», утвердить и править НЕ может
    login(client, "buh", "buh123")
    page = client.get(f"/payroll/weeks?d={WEEK}").text
    assert "Андрей Демченко" in page and "Проверено" in page
    client.post(f"/payroll/weeks/{WEEK}/status", data={"status": "checked", "note": "сверила с нарядами"})
    assert _week().status == "checked"
    r = client.post(f"/payroll/weeks/{WEEK}/status", data={"status": "approved"})
    assert r.status_code == 403 and _week().status == "checked"
    r = client.post(f"/payroll/weeks/{WEEK}/adjust", data={"user_id": str(andrey.id), "amount": "200", "kind": "bonus", "note": "x"})
    assert r.status_code == 403
    # Выдать до утверждения нельзя
    client.post(f"/payroll/weeks/{WEEK}/pay", data={"user_id": str(andrey.id)})
    with SessionLocal() as db:
        assert not db.scalar(select(PayrollPayout).where(PayrollPayout.week_id == _week().id))
    client.post("/logout")

    # Админ: премия Андрею, реквизиты, утверждает
    login(client, "admin", "admin123")
    client.post(f"/payroll/weeks/{WEEK}/adjust", data={"user_id": str(andrey.id), "amount": "200", "kind": "bonus",
                                                      "note": "за срочный ремонт"})
    client.post(f"/payroll/weeks/{WEEK}/adjust", data={"user_id": str(sergey.id), "amount": "100", "kind": "fine",
                                                      "note": "опоздание"})
    client.post(f"/payroll/account/{andrey.id}", data={"pay_method": "card", "pay_account": "4149 0000 0000 1234", "pay_bank": "Приват"})
    # Утверждение — по каждому отдельно (11.11.2026): «утвердить всех» больше нет
    assert client.post(f"/payroll/weeks/{WEEK}/status", data={"status": "approved"}).status_code == 403
    client.post(f"/payroll/weeks/{WEEK}/people/{andrey.id}", data={"status": "approved"})
    assert _week().status == "checked"           # Сергей ещё не утверждён
    page = client.get(f"/payroll/weeks?d={WEEK}").text
    assert "Утвердить к выплате" in page and "Выдать всем" not in page and "Расчётный лист" in page
    client.post("/logout")
    with SessionLocal() as db:
        d = wsvc.week_data(db, andrey.branch_id, WEEK, [db.get(User, andrey.id), db.get(User, sergey.id)])
        assert d["rows"][andrey.id]["accrued"] == Decimal("1200.00") and d["rows"][sergey.id]["accrued"] == Decimal("400.00")
        assert db.get(User, andrey.id).pay_account == "4149000000001234"

    # Бухгалтер выдаёт: Андрею — на карту, потом всем остальным
    login(client, "buh", "buh123")
    client.post(f"/payroll/weeks/{WEEK}/pay", data={"user_id": str(andrey.id), "method": "card"})
    with SessionLocal() as db:
        p = db.scalar(select(PayrollPayout).where(PayrollPayout.week_id == _week().id, PayrollPayout.user_id == andrey.id))
        assert p.amount == Decimal("1200.00") and "1234" in p.account
    # Сергея ещё не утвердили — выдать ему нельзя
    client.post(f"/payroll/weeks/{WEEK}/pay", data={"user_id": str(sergey.id)})
    with SessionLocal() as db:
        assert not db.scalar(select(PayrollPayout).where(PayrollPayout.week_id == _week().id, PayrollPayout.user_id == sergey.id))
    client.post("/logout")
    login(client, "admin", "admin123")
    client.post(f"/payroll/weeks/{WEEK}/people/{sergey.id}", data={"status": "approved"})
    assert _week().status == "approved"
    client.post("/logout")
    login(client, "buh", "buh123")
    client.post(f"/payroll/weeks/{WEEK}/pay", data={"user_id": str(sergey.id)})
    assert _week().status == "paid"
    # Расчётный лист: один сотрудник и все
    sheet = client.get(f"/payroll/weeks/{WEEK}/sheet?user={andrey.id}").text
    assert "РАСЧЁТНЫЙ ЛИСТ" in sheet and "Андрей Демченко" in sheet and "Сергей Ключников" not in sheet
    assert client.get(f"/payroll/weeks/{WEEK}/sheet").text.count('class="sheet"') == 2
    with SessionLocal() as db:
        users = [db.get(User, uid) for uid in (andrey.id, sergey.id)]
        d = wsvc.week_data(db, andrey.branch_id, WEEK, users)
        assert d["rows"][sergey.id]["due"] == 0 and d["rows"][andrey.id]["due"] == 0
    client.post("/logout")

    # Админ дал бухгалтеру право исправлять → теперь может исправить начисление (с причиной)
    with SessionLocal() as db:
        current_branch_id.set(buh.branch_id)
        db.add(UserPermission(user_id=buh.id, permission="payroll.edit", allow=True, branch_id=buh.branch_id))
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.user_id == sergey.id, PayrollAccrual.on_date == WEEK.replace(day=6)))
        db.commit()
    login(client, "buh", "buh123")
    client.post(f"/payroll/accruals/{a.id}", data={"amount": "550", "reason": "пересчитала по нормочасам"})
    with SessionLocal() as db:
        a = db.get(PayrollAccrual, a.id)
        assert a.amount == Decimal("550.00") and "пересчитала" in a.note
        # Доначислили после выплаты — неделя снова «утверждена», у Сергея долг 50
        users = [db.get(User, uid) for uid in (andrey.id, sergey.id)]
        assert wsvc.week_data(db, andrey.branch_id, WEEK, users)["rows"][sergey.id]["due"] == Decimal("50.00")
    assert _week().status == "approved"
    for u in ("/payroll", "/payroll/weeks", f"/payroll/weeks?d={WEEK}", "/payroll?p=month"):
        assert client.get(u).status_code == 200, u
    client.post("/logout")

    # Слесарь видит только свою неделю
    login(client, "andrey", "andrey123")
    page = client.get(f"/payroll/weeks?d={WEEK}").text
    assert "Андрей Демченко" in page and "Сергей Ключников" not in page and "Выдать всем" not in page
    client.post("/logout")
