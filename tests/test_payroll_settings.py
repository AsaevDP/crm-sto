"""Схемы и оклад (11.11.2026): плитки должностей, внутри — сотрудники; оклад можно добавить, исправить в строке и убрать."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PayScheme, PaySchemeLine, User, UserPayScheme

from .test_step6 import _as


def _lines(login_):
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == login_))
        ids = db.scalars(select(UserPayScheme.scheme_id).where(UserPayScheme.user_id == u.id)).all()
        return u, db.scalars(select(PaySchemeLine).where(PaySchemeLine.scheme_id.in_(ids))).all()


def test_salary_add_edit_delete(client):
    _as(client, "admin")
    page = client.get("/settings/payroll").text
    assert 'href="/settings/payroll?role=mechanic"' in page and "Выберите должность" in page
    page = client.get("/settings/payroll?role=mechanic").text
    assert "Сергей Ключников" in page and "Ирина" not in page   # только слесари

    u, before = _lines("sergey")
    r = client.post(f"/settings/payroll/{u.id}/lines", data={"kind": "salary", "salary_period": "day", "rate": "650"},
                    follow_redirects=False)
    assert r.status_code == 303 and "role=mechanic" in r.headers["location"]
    _, after = _lines("sergey")
    line = next(l for l in after if l.kind == "salary" and l not in before and l.rate == Decimal("650"))
    assert "Оклад 650 ₴" in client.get("/settings/payroll?role=mechanic").text

    # Исправить сумму в строке
    r = client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "720,5", "role": "mechanic"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/payroll?role=mechanic"
    with SessionLocal() as db:
        assert db.get(PaySchemeLine, line.id).rate == Decimal("720.5")
    assert "Оклад 720,5 ₴" in client.get("/settings/payroll?role=mechanic").text

    # Не админ — нельзя
    _as(client, "buh")
    assert client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "1"}).status_code == 403

    # По строке уже начисляли оклад (как у Игоря Мельника) — «Убрать» всё равно работает, начисление остаётся
    from datetime import date
    from app.db import current_branch_id
    from app.models import PayrollAccrual
    with SessionLocal() as db:
        current_branch_id.set(u.branch_id)
        db.add(PayrollAccrual(user_id=u.id, scheme_line_id=line.id, base_amount=Decimal(1), rate=line.rate,
                              amount=Decimal("720.50"), on_date=date(2031, 5, 5), kind="salary", note="Оклад (тест)"))
        db.commit()
    # Ошиблись с окладом: 720,50 → 700. Невыданное начисление пересчитывается само (1 день × 700)
    _as(client, "admin")
    client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "700", "role": "mechanic"})
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.note.like("Оклад (тест)%")))
        assert a.amount == Decimal("700.00") and "ставка исправлена" in a.note
    # Деньги за ту неделю уже выдали — начисление не трогаем, подсказываем исправить в «Зарплате»
    from app.models import PayrollPayout
    from app.services import payweek as wsvc
    with SessionLocal() as db:
        current_branch_id.set(u.branch_id)
        wk = wsvc.get_week(db, u.branch_id, date(2031, 5, 5), create=True)
        db.add(PayrollPayout(user_id=u.id, period_from=date(2031, 5, 5), period_to=date(2031, 5, 11), amount=Decimal("700"),
                             paid_on=date(2031, 5, 12), method="cash", week_id=wk.id))
        db.commit()
    page = client.post(f"/settings/payroll/lines/{line.id}", data={"rate": "650", "role": "mechanic"}).text
    assert "уже выданы" in page
    with SessionLocal() as db:
        assert db.scalar(select(PayrollAccrual).where(PayrollAccrual.note.like("Оклад (тест)%"))).amount == Decimal("700.00")
    r = client.post(f"/settings/payroll/lines/{line.id}/delete", data={"role": "mechanic"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/payroll?role=mechanic"
    with SessionLocal() as db:
        assert db.get(PaySchemeLine, line.id) is None
        assert db.scalar(select(PayrollAccrual).where(PayrollAccrual.note.like("Оклад (тест)%"))).amount == Decimal("700.00")
