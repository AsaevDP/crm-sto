"""20.11.2026 (брат): «Моя зарплата» и «Зарплата сотрудников».

- Бухгалтер: «Моя зарплата» + «Зарплата сотрудников» (проверить, выдать).
- Директор: «Зарплата сотрудников» закрыта, пока админ не поставит галочку (payroll.view_all); с ней — видит всех,
  🟠 «утвердить к выплате», кнопка «Утвердить».
- Зам: закрыто → «Попросить доступ» → у директора 🔴 → разрешил на неделю → видит и утверждает → директор закрыл → снова закрыто.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, User, UserPermission
from app.models.payroll import PayrollAccess, PayWeek, PayWeekPerson
from app.services import payweek as wsvc

from .conftest import login


def _menu(client):
    side = client.get("/more").text
    return re.findall(r'href="(/payroll[a-z/]*)"', side)


def _badges(client, href="/payroll/weeks"):
    return client.get("/api/notify").json()["badges"].get(href, [])


def _setup_week():
    """Начисление «сергею» за прошлую неделю — чтобы было что проверять и утверждать."""
    start = wsvc.monday(date.today()) - timedelta(days=7)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == "sergey"))
        current_branch_id.set(u.branch_id)
        db.add(PayrollAccrual(user_id=u.id, amount=Decimal("1500"), on_date=start + timedelta(days=1), kind="salary",
                              note="тест доступа к зарплате"))
        db.commit()
        return start, u.id


def _pst(start, uid):
    with SessionLocal() as db:
        w = db.scalar(select(PayWeek).where(PayWeek.week_start == start))
        p = db.scalar(select(PayWeekPerson).where(PayWeekPerson.week_id == w.id, PayWeekPerson.user_id == uid)) if w else None
        return p.status if p else "open"


def test_accountant_director_deputy(client):
    start, uid = _setup_week()
    d = start.isoformat()

    # Бухгалтер: два пункта, проверяет
    login(client, "buh", "buh123")
    assert "/payroll/my" in _menu(client) and "/payroll/weeks" in _menu(client)
    assert any(x["c"] == "orange" and "проверить" in x["t"] for x in _badges(client))
    client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "checked"})
    assert _pst(start, uid) == "checked"
    assert client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "approved"},
                       follow_redirects=False).status_code == 403   # утверждает не бухгалтер
    client.post("/logout")

    # Директор без галочки админа: раздел закрыт, утвердить нельзя
    login(client, "director", "director123")
    assert "/payroll/weeks" in _menu(client) and "/payroll/my" in _menu(client)
    assert "Раздел закрыт" in client.get("/payroll/weeks").text
    assert client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "approved"},
                       follow_redirects=False).status_code == 403
    client.post("/logout")
    with SessionLocal() as db:   # админ ставит директору галочку «Зарплата сотрудников»
        dr = db.scalar(select(User).where(User.login == "director"))
        current_branch_id.set(dr.branch_id)
        db.add(UserPermission(user_id=dr.id, permission="payroll.view_all", allow=True))
        db.commit()
    login(client, "director", "director123")
    assert any(x["c"] == "orange" and "утвердить" in x["t"] for x in _badges(client))
    page = client.get(f"/payroll/weeks?d={d}").text
    assert "Утвердить к выплате" in page and "Зарплата сотрудников" in page
    client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "approved"})
    assert _pst(start, uid) == "approved"
    client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "open"})   # вернуть — для проверки зама
    assert _pst(start, uid) == "open"
    client.post("/logout")
    login(client, "buh", "buh123")
    client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "checked"})
    client.post("/logout")

    # Зам: закрыто → просит доступ
    login(client, "deputy", "deputy123")
    page = client.get("/payroll/weeks").text
    assert "Попросить доступ" in page
    client.post("/payroll/access-request", data={"reason": "директор в отпуске"})
    assert "Запрос отправлен" in client.get("/payroll/weeks").text
    client.post("/logout")
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccess).where(PayrollAccess.status == "pending"))
    login(client, "director", "director123")
    assert any(x["c"] == "red" and "зам просит" in x["t"] for x in _badges(client))
    assert "Зарплата сотрудников — доступ заму" in client.get("/access-requests").text
    client.post(f"/payroll-access/{a.id}", data={"action": "approve", "hours": "168"})
    client.post("/logout")

    login(client, "deputy", "deputy123")
    page = client.get(f"/payroll/weeks?d={d}").text
    assert "Утвердить к выплате" in page
    client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "approved"})
    assert _pst(start, uid) == "approved"
    client.post("/logout")

    login(client, "director", "director123")   # закрыть досрочно
    client.post(f"/payroll-access/{a.id}", data={"action": "close"})
    client.post("/logout")
    login(client, "deputy", "deputy123")
    assert "Попросить доступ" in client.get("/payroll/weeks").text
    assert client.post(f"/payroll/weeks/{d}/people/{uid}", data={"status": "open"},
                       follow_redirects=False).status_code == 403
    client.post("/logout")

    # Бухгалтер выдаёт утверждённое
    login(client, "buh", "buh123")
    assert any(x["c"] == "green" and "выдать" in x["t"] for x in _badges(client))
    client.post("/logout")
    with SessionLocal() as db:   # убрать галочку директора — база в тестах общая
        for p in db.scalars(select(UserPermission).where(UserPermission.permission == "payroll.view_all")):
            db.delete(p)
        db.commit()


def test_mechanic_my_salary(client):
    login(client, "andrey", "andrey123")
    assert set(_menu(client)) == {"/payroll/my"}
    assert client.get("/payroll/my").status_code == 200
    r = client.get("/payroll/weeks", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"].endswith("/payroll/my")
    client.post("/logout")
