"""26.11.2026 (брат): зарплата — «Сегодня» = за день, «Весь период»; табель бухгалтера — «Забыли отметить уход»
(вписать время) и «Для сведения» (решает директор) вместо общего «Нужно решить», одна цифра в плитке и кружке."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, User

from .conftest import login


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def test_payroll_today_and_all_period(client):
    today = date.today()
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == "sergey"))
        current_branch_id.set(u.branch_id)
        db.add(PayrollAccrual(user_id=u.id, branch_id=u.branch_id, amount=Decimal("111"), base_amount=Decimal("111"),
                              rate=0, kind="percent", note="Работа сегодня (тест)", on_date=today))
        db.add(PayrollAccrual(user_id=u.id, branch_id=u.branch_id, amount=Decimal("222"), base_amount=Decimal("222"),
                              rate=0, kind="percent", note="Работа давно (тест)", on_date=today - timedelta(days=200)))
        db.commit()
        uid = u.id
    _as(client, "buh")
    r = client.get(f"/payroll/people/{uid}", follow_redirects=False)
    assert "p=all" in r.headers["location"]
    allp = client.get(f"/payroll?user={uid}&p=all").text
    assert "Весь период" in allp and "Сегодня" in allp and "Работа давно (тест)" in allp and "Работа сегодня (тест)" in allp
    day = client.get(f"/payroll?user={uid}&p=day").text
    assert "Работа сегодня (тест)" in day and "Работа давно (тест)" not in day
    assert "data-pkey=\"today\"" not in day   # ссылки-прыжка «Сегодня» больше нет — есть кнопка «Сегодня» (за день)


def test_accountant_timesheet_tiles(client):
    _as(client, "buh")
    page = client.get("/timesheet").text
    assert "Нужно решить" not in page and "Забыли отметить уход" in page and "Для сведения" in page
    badges = client.get("/api/notify").json()["badges"].get("/timesheet", [])
    for b in badges:
        assert "v=boss" not in b["href"]
    # Директору «Нужно решить» по-прежнему есть
    _as(client, "director")
    assert "Нужно решить" in client.get("/timesheet").text
    assert datetime.combine(date.today(), time()) and True
