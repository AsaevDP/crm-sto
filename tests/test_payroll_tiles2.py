"""26.11.2026 (брат): «Выданные» в «Зарплате сотрудников», неделя одного сотрудника отдельной страницей, карточка
сотрудника — «заработал − выдано = осталось», «Из чего сложилось», начисления по дням (раскрываются)."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, User

from .conftest import login

WEEK = date(2031, 7, 7)   # понедельник в будущем


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def test_paid_tile_single_week_and_days(client):
    with SessionLocal() as db:
        a = db.scalar(select(User).where(User.login == "andrey"))
        current_branch_id.set(a.branch_id)
        for day, amt, note in ((1, "700", "Замена колодок"), (1, "300", "Замена масла"), (3, "-200", "Удержание (тест)")):
            db.add(PayrollAccrual(user_id=a.id, branch_id=a.branch_id, amount=Decimal(amt), base_amount=abs(Decimal(amt)),
                                  rate=0, kind="deduction" if amt.startswith("-") else "percent", note=note,
                                  on_date=WEEK + timedelta(days=day)))
        db.commit()
        aid = a.id
    # Неделя одного сотрудника — отдельная страница, без общего списка
    _as(client, "admin")
    page = client.get(f"/payroll/weeks/{WEEK}/people/{aid}").text
    assert "· неделя" in page and f'id="u-{aid}"' in page and "pw-people" not in page
    assert f"/payroll/weeks/{WEEK - timedelta(days=7)}/people/{aid}" in page   # стрелки — по неделям этого сотрудника
    # Проверил / утвердил / выдал — со страницы недели сотрудника туда же и возвращает
    ref = {"referer": f"http://testserver/payroll/weeks/{WEEK}/people/{aid}"}
    r = client.post(f"/payroll/weeks/{WEEK}/people/{aid}", data={"status": "approved"}, headers=ref, follow_redirects=False)
    assert r.headers["location"] == f"/payroll/weeks/{WEEK}/people/{aid}"
    r = client.post(f"/payroll/weeks/{WEEK}/pay", data={"user_id": str(aid)}, headers=ref, follow_redirects=False)
    assert r.headers["location"] == f"/payroll/weeks/{WEEK}/people/{aid}"
    # «Выданные»: только закрытые
    paid = client.get(f"/payroll/weeks?d={WEEK}&f=paid").text
    assert "Выданные" in paid and f'id="u-{aid}"' in paid
    assert f'id="u-{aid}"' not in client.get(f"/payroll/weeks?d={WEEK}&f=approved").text
    # Карточка сотрудника: верх, «Из чего сложилось», дни
    card = client.get(f"/payroll?user={aid}&f={WEEK}&t={WEEK + timedelta(days=6)}").text
    # выдали сегодня, а период — будущая неделя: в этом периоде выплат нет
    assert "Заработал" in card and "Осталось выдать" in card and "Из чего сложилось" in card and "800,00" in card
    assert "По дням" in card and card.count('class="pay-day"') == 2 and "Замена колодок" in card
    assert f"/payroll/weeks/{WEEK}/people/{aid}" in card
