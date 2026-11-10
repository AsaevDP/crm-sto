"""Правки Стаса от 08.11 (docs/next-tasks.md): запись, выполнение работ, печать, категории склада и разбора,
акт приёма, директор и зам, подъёмник слесаря, подробная зарплата."""
import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Appointment, Lift, PartCatalog, User, WoWork
from app.models.orders import INTAKE_ITEMS
from app.models.stock import guess_category, guess_razbor_category, guess_section
from app.services.orders import work_progress

from .test_step6 import _as, _order_to_work, _works, _wo


def _uid(login):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login))


def test_intake_items_only_docs_and_valuables():
    # Решение владельца 09.11.2026: в акте приёма только документы и ценные вещи, остальное — заметкой
    from app.models.orders import INTAKE_LABELS
    assert list(INTAKE_ITEMS) == ["docs", "valuables"]
    assert INTAKE_LABELS["spare"] == "Запаска"  # старые наряды показываются как раньше


def test_category_guess():
    assert guess_section("Масло моторное 5W-30") == "to"
    assert guess_category("Фильтр масляный", "to") == "Фильтры"
    assert guess_section("Колодки тормозные передние") == "repair"
    assert guess_category("Колодки тормозные передние", "repair") == "Тормоза"
    assert guess_razbor_category("Фара передняя левая") == "Оптика"
    assert guess_razbor_category("Что-то странное") == "Прочее"


def test_stock_sections_filter(client):
    url = _order_to_work(client, "МН 7001 МН", "0507001000", "Фильтр масляный (категории)")
    _as(client, "pavel")
    assert client.get("/stock?sec=to").status_code == 200
    assert "Фильтр масляный (категории)" in client.get("/stock?sec=to&only=all").text
    assert "Фильтр масляный (категории)" not in client.get("/stock?sec=repair&only=all").text
    with SessionLocal() as db:
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == "Фильтр масляный (категории)"))
        assert cat.section == "to" and cat.category == "Фильтры"
    assert url


def test_progress_done_by_and_undo(client):
    url = _order_to_work(client, "МН 7002 МН", "0507002000", "Колодки (прогресс)")
    wo = _wo(url)
    with SessionLocal() as db:
        p = work_progress(db, [wo.id])[wo.id]
    assert p["level"] == "zero" and p["total"] == 1
    _as(client, "irina")
    client.post(f"{url}/works", data={"name": "Диагностика ходовой", "pricing": "fixed", "rate": "300", "qty": "1"})
    w1, w2 = _works(url)
    client.post(f"{url}/works/{w1.id}/state", data={"action": "done"})
    with SessionLocal() as db:
        assert work_progress(db, [wo.id])[wo.id]["level"] == "part"
        assert db.get(WoWork, w1.id).done_by == _uid("irina")
    assert "wprog" in client.get("/orders").text  # полоска на карточке машины
    # Менеджер может вернуть отметку
    client.post(f"{url}/works/{w1.id}/state", data={"action": "undo"})
    with SessionLocal() as db:
        assert db.get(WoWork, w1.id).status == "todo"


def test_mechanic_takes_lift(client):
    _as(client, "andrey")
    with SessionLocal() as db:
        free = db.scalar(select(Lift).where(Lift.mechanic_id.is_(None)).order_by(Lift.number))
        sergey_lift = db.scalar(select(Lift).where(Lift.mechanic_id == _uid("sergey")))
    assert "Мой подъёмник" in client.get("/orders").text
    client.post("/lifts/take", data={"lift_id": str(free.id)})
    with SessionLocal() as db:
        mine = db.scalars(select(Lift).where(Lift.mechanic_id == _uid("andrey"))).all()
    assert [l.id for l in mine] == [free.id]
    # Чужой пост забрать нельзя
    client.post("/lifts/take", data={"lift_id": str(sergey_lift.id)})
    with SessionLocal() as db:
        assert db.get(Lift, sergey_lift.id).mechanic_id == _uid("sergey")
    # Менеджеру выбора нет (право только у слесаря)
    _as(client, "irina")
    assert client.post("/lifts/take", data={"lift_id": str(free.id)}).status_code == 403


def test_schedule_booking_to_intake(client):
    _as(client, "irina")
    d = (date.today() + timedelta(days=1)).isoformat()
    mech = str(_uid("andrey"))
    r = client.post("/schedule", data={"day": d, "time": "10:00", "duration": "60", "mechanic_id": mech,
                                       "client_name": "Пётр Записанный", "phone": "0671112233", "car": "Skoda Octavia",
                                       "plate": "КА 1111 КА", "work": "ТО-60"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/schedule?d={d}").text
    assert "Пётр Записанный" in page and "10:00–11:00" in page
    # Пересечение у того же мастера — предупреждение
    r = client.post("/schedule", data={"day": d, "time": "10:30", "duration": "30", "mechanic_id": mech, "client_name": "Второй"})
    assert "пересечение" in r.text
    assert "Неделя" in client.get(f"/schedule?d={d}&view=week").text
    with SessionLocal() as db:
        a = db.scalar(select(Appointment).where(Appointment.client_name == "Пётр Записанный"))
    r = client.post(f"/schedule/{a.id}/status", data={"status": "arrived"}, follow_redirects=False)
    assert r.headers["location"] == f"/orders/new?appt={a.id}"
    intake = client.get(r.headers["location"]).text
    assert "Приёмка по записи" in intake and "Skoda" in intake
    # Слесарь видит запись, но не может записывать
    _as(client, "andrey")
    assert "Пётр Записанный" in client.get(f"/schedule?d={d}").text
    assert client.post("/schedule", data={"day": d, "client_name": "x"}).status_code == 403
    _as(client, "sergey")
    assert "Пётр Записанный" not in client.get(f"/schedule?d={d}").text


def test_payroll_detail_payout_print(client):
    _as(client, "admin")
    andrey = _uid("andrey")
    page = client.get("/payroll?p=month")   # сводка за период (главный экран с 11.11.2026 — недели)
    assert page.status_code == 200 and "К выплате" in page.text
    client.post("/payroll/payouts", data={"user_id": str(andrey), "amount": "100", "method": "cash", "note": "аванс"})
    page = client.get(f"/payroll?user={andrey}").text
    assert "аванс" in page and "100,00" in page
    assert "РАСЧЁТНЫЙ ЛИСТ" in client.get(f"/payroll/print?user={andrey}").text
    assert "ВЕДОМОСТЬ" in client.get("/payroll/print").text
    csv = client.get("/payroll/export.csv")
    assert csv.status_code == 200 and "Сотрудник;Должность" in csv.content.decode("utf-8-sig")


def test_folders_print(client):
    url = _order_to_work(client, "МН 7003 МН", "0507003000", "Колодки (папка)")
    wo = _wo(url)
    _as(client, "irina")
    html = client.get(f"/clients/{wo.client_id}/print").text
    assert "ПАПКА КЛИЕНТА" in html and wo.number in html
    assert "ПАПКА СОТРУДНИКА" in client.get(f"/staff/{_uid('irina')}/print").text
    # Слесарь — только свою папку
    _as(client, "andrey")
    assert client.get(f"/staff/{_uid('andrey')}/print").status_code == 200
    assert client.get(f"/staff/{_uid('irina')}/print").status_code == 403


def test_director_and_deputy_cabinets(client):
    client.post("/logout")
    from .conftest import login
    login(client, "director", "director123")
    assert client.get("/", follow_redirects=False).headers["location"] == "/reports"
    for tab in ("overview", "quarters", "staff", "accounting"):
        r = client.get(f"/reports?tab={tab}")
        assert r.status_code == 200, tab
    assert "Валовая прибыль" in client.get("/reports").text
    assert client.get("/reports?period=year").status_code == 200
    assert client.get("/reports?period=quarter&n=4").status_code == 200
    assert "ОТЧЁТ" in client.get("/reports/print?tab=quarters").text
    assert client.get("/reports/export.csv?tab=quarters").status_code == 200
    # Зам: без прибыли и бухгалтерии
    client.post("/logout")
    login(client, "deputy", "deputy123")
    html = client.get("/reports?tab=accounting").text
    assert "Валовая прибыль" not in html and "Бухгалтерия" not in html
    # Слесарь в кабинет не попадает
    _as(client, "andrey")
    assert client.get("/reports").status_code == 403


def test_razbor_category_filter(client):
    _as(client, "dmitro")
    r = client.post("/razbor/donors", data={"make": "BMW", "model": "X5", "year": "2015"}, follow_redirects=False)
    did = r.headers["location"].rsplit("/", 1)[1]
    client.post(f"/razbor/donors/{did}/parts", data={"name": "Фара левая (кат)", "price": "5000", "qty": "1"})
    client.post(f"/razbor/donors/{did}/parts", data={"name": "Дверь задняя (кат)", "price": "3000", "qty": "1",
                                                     "category": "Кузов"})
    page = client.get("/razbor?cat=Оптика").text
    assert "Фара левая (кат)" in page and "Дверь задняя (кат)" not in page
    assert "Дверь задняя (кат)" in client.get("/razbor?cat=Кузов").text
    assert uuid.UUID(did)
