"""26.11.2026 (брат): «Зарплата сотрудников» — плитки Все / Проверить / Утвердить / Выдать фильтруют список;
плитка каждого сотрудника открывает его историю: по неделям (начислено, выдано, не выдано, этап) и работы."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import PayrollAccrual, User
from app.services import payweek as wsvc

from .conftest import login

WEEK = date(2031, 6, 2)   # понедельник в будущем — своя неделя, не мешает другим тестам


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def test_tiles_filter_and_person_history(client):
    with SessionLocal() as db:
        a = db.scalar(select(User).where(User.login == "andrey"))
        s = db.scalar(select(User).where(User.login == "sergey"))
        current_branch_id.set(a.branch_id)
        for u, amt in ((a, "1200"), (s, "900")):
            db.add(PayrollAccrual(user_id=u.id, branch_id=u.branch_id, amount=Decimal(amt), base_amount=Decimal(amt),
                                  rate=0, kind="percent", note="работа (тест плиток)", on_date=WEEK + timedelta(days=1)))
        db.commit()
        aid, sid, an, sn = a.id, s.id, a.full_name, s.full_name
    _as(client, "buh")
    # Бухгалтер проверил Андрея — он в «Утвердить», Сергей остаётся в «Проверить»
    client.post(f"/payroll/weeks/{WEEK}/people/{aid}", data={"status": "checked"})
    page = client.get(f"/payroll/weeks?d={WEEK}").text
    assert "Все" in page and "pw-people" in page and f"/payroll/people/{aid}" in page
    assert f'id="u-{aid}"' in page and f'id="u-{sid}"' in page
    p_open = client.get(f"/payroll/weeks?d={WEEK}&f=open").text
    assert f'id="u-{sid}"' in p_open and f'id="u-{aid}"' not in p_open
    p_chk = client.get(f"/payroll/weeks?d={WEEK}&f=checked").text
    assert f'id="u-{aid}"' in p_chk and f'id="u-{sid}"' not in p_chk
    p_app = client.get(f"/payroll/weeks?d={WEEK}&f=approved").text
    assert f'id="u-{aid}"' not in p_app and "Здесь никого нет" in p_app
    # Плитка сотрудника → история
    r = client.get(f"/payroll/people/{aid}", follow_redirects=False)
    assert r.status_code == 303 and f"user={aid}" in r.headers["location"]
    hist = client.get(f"/payroll?user={aid}&f={WEEK}&t={WEEK + timedelta(days=6)}").text
    assert "По неделям · 1" in hist and an in hist and "проверено" in hist and "1 200,00" in hist
    # Сотрудник чужую историю не открывает
    _as(client, "andrey")
    assert client.get(f"/payroll/people/{sid}").status_code == 403
    assert wsvc.monday(WEEK) == WEEK and sn
