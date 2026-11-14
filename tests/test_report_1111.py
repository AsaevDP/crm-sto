"""Отчёт тестировщика 11.11.2026 + «в долг не работаем»: на каждую найденную ошибку — проверка."""
import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (Client, IssueRequest, Payment, PayrollAccrual, PurchaseLine, StockItem, User, Vehicle, WoPart,
                        WorkOrder, WoWork)
from app.services import stock as ssvc

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _order_to_work, _wo, _works


def _uid(login):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login))


def _parts(url):
    with SessionLocal() as db:
        return db.scalars(select(WoPart).where(WoPart.work_order_id == _wo(url).id).order_by(WoPart.created_at)).all()


def _free(part_name):
    with SessionLocal() as db:
        from app.models import PartCatalog
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == part_name))
        return ssvc.catalog_free(db, _wo_branch(db), [cat.id])[cat.id]


def _wo_branch(db):
    from app.models import Branch
    return db.scalar(select(Branch.id))


def _on_hand(part_name):
    with SessionLocal() as db:
        from app.models import PartCatalog
        cat = db.scalar(select(PartCatalog).where(PartCatalog.name == part_name))
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat.id)).all()
        bal = ssvc.balances(db, _wo_branch(db), [i.id for i in items])
        return sum((bal[i.id]["on_hand"] for i in items), Decimal(0)), sum((bal[i.id]["reserved"] for i in items), Decimal(0))


def _ready(client, plate, phone, part, price="1000"):
    """Наряд «Готово»: работа (фикс price) + запчасть 200 (закупка 100, наценка 100 %), выдана слесарю."""
    url = _order_to_work(client, plate, phone, part)
    _as(client, "irina")
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "fixed", "rate": price, "qty": "1"})
    _as(client, "andrey")
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    assert _wo(url).status == "ready"
    return url


def _n_payments(url):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Payment).where(Payment.work_order_id == _wo(url).id))


# ---------- Деньги ----------
def test_double_payment_overpay_and_no_debt(client):
    url = _ready(client, "КЕ 1101 КЕ", "0501101000", "Колодки (двойная оплата)")
    _as(client, "irina")
    # Два нажатия одной формы — один платёж
    tok = uuid.uuid4().hex
    for _ in range(2):
        client.post(f"{url}/payments", data={"amount": "100", "method": "cash", "purpose": "extra", "token": tok})
    assert _n_payments(url) == 1
    # Переплата не принимается (итог 1200, оплачено 100)
    client.post(f"{url}/payments", data={"amount": "5000", "method": "cash", "purpose": "full"})
    assert _n_payments(url) == 1
    # С долгом машину не выдаём — никому
    _as(client, "admin")
    client.post(f"{url}/close", data={"state": "ok", "debt_comment": "потом"})
    assert _wo(url).status == "ready"
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "1100", "method": "card", "purpose": "full"})
    assert _wo(url).status == "paid"
    # Оплаченный: цены не меняются (наценка запчастиста), оплату больше не принять
    _as(client, "oleg")
    p = _parts(url)[0]
    r = client.post(f"{url}/parts/{p.id}/markup", data={"markup_pct": "300"}, headers={"x-requested-with": "fetch"})
    assert r.status_code == 400 and _parts(url)[0].price == Decimal("200.00")
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "1", "method": "cash", "purpose": "extra"})
    assert _n_payments(url) == 2
    # Оплаченный наряд не отменяется
    client.post(f"{url}/cancel", data={"reason": "передумал"})
    assert _wo(url).status == "paid"
    client.post(f"{url}/close", data={"state": "ok"})
    assert _wo(url).status == "closed"


def test_two_discounts_and_negative_prices(client):
    url = _ready(client, "КЕ 1102 КЕ", "0501102000", "Колодки (две скидки)", price="1000")
    _as(client, "irina")
    # Отрицательная цена работы не проходит
    client.post(f"{url}/works", data={"name": "Минус", "pricing": "fixed", "rate": "-900", "qty": "1"})
    w = [x for x in _works(url) if x.name == "Минус"][0]
    assert w.price == Decimal("0.00")
    client.post(f"{url}/works/{w.id}", data={"delete": "1"})
    assert _wo(url).status == "ready"   # удалили несделанную работу — наряд снова «Готово», а не завис в «В работе»
    from app.services import money as msvc
    # Работы 1000, запчасти 200: скидка на работы 10 % → 1100; ещё 10 % на весь наряд → 990
    client.post(f"{url}/money", data={"works_discount_pct": "10"})
    with SessionLocal() as db:
        t = msvc.totals(db, _wo(url))
    assert (t.works_discount, t.total) == (Decimal("100.00"), Decimal("1100.00"))
    client.post(f"{url}/money", data={"discount_pct": "10"})
    with SessionLocal() as db:
        t = msvc.totals(db, _wo(url))
    assert (t.order_discount, t.total) == (Decimal("110.00"), Decimal("990.00"))
    # Частичная оплата 900, потом скидка закрывает остаток → «Оплачено» и зарплата (раньше зависал в «Готово»)
    client.post(f"{url}/money", data={"discount_pct": "0"})                  # итог 1100
    client.post(f"{url}/payments", data={"amount": "900", "method": "cash", "purpose": "extra"})
    assert _wo(url).status == "ready"
    client.post(f"{url}/money", data={"works_discount_pct": "50"})           # 500 + 200 = 700 < оплачено 900 — нельзя
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).total == Decimal("1100.00")
    client.post(f"{url}/money", data={"works_discount_pct": "30"})           # 700 + 200 = 900 = оплачено
    assert _wo(url).status == "paid"
    # Слесарь: 30 % от работ после скидки на работы: 1000 × 0.7 × 0.3 = 210
    with SessionLocal() as db:
        a = db.scalar(select(PayrollAccrual).where(PayrollAccrual.work_order_id == _wo(url).id,
                                                   PayrollAccrual.wo_work_id.is_not(None)))
    assert a.amount == Decimal("210.00")


def test_refund_and_cancel_returns_everything(client):
    # Наряд в работе: запчасть выдана слесарю, внесена предоплата
    url = _order_to_work(client, "КЕ 1103 КЕ", "0501103000", "Диск (отмена)")
    _as(client, "irina")
    client.post(f"{url}/money", data={"payment_type": "prepay", "prepay_amount": "500", "prepay_for": "диск"})
    client.post(f"{url}/payments", data={"amount": "500", "method": "cash", "purpose": "prepay"})
    hand_before, _ = _on_hand("Диск (отмена)")
    # С деньгами просто так не отменить
    client.post(f"{url}/cancel", data={"reason": "Клиент забрал машину"})
    assert _wo(url).status == "work"
    # Возврат: больше оплаченного нельзя, без причины нельзя
    client.post(f"{url}/refund", data={"amount": "600", "method": "cash", "reason": "отмена"})
    client.post(f"{url}/refund", data={"amount": "500", "method": "cash", "reason": ""})
    assert _n_payments(url) == 1
    client.post(f"{url}/refund", data={"amount": "500", "method": "cash", "reason": "Клиент передумал"})
    from app.services import money as msvc
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).paid == 0
    client.post(f"{url}/cancel", data={"reason": "Клиент забрал машину"})
    assert _wo(url).status == "cancel"
    hand_after, _ = _on_hand("Диск (отмена)")
    assert hand_after == hand_before + 1   # выданная деталь вернулась на склад
    assert "Возвраты клиентам" in client.get("/money").text


def test_cancel_releases_reserve_and_ordered_part_goes_free(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КЕ 1104 КЕ", phone="0501104000", works="ТО").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": "Фильтр (отмена заказа)", "qty": "2", "cost": "50", "action": "order"})
    agree(client, url)   # 13.11.2026: подбор завершён → менеджер согласовал → заказ
    with SessionLocal() as db:
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == _parts(url)[0].id))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    _as(client, "irina")
    client.post(f"{url}/cancel", data={"reason": "Не дождался"})
    assert _wo(url).status == "cancel"
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-1"})
    # Пришло после отмены — на свободный склад, а не в резерв отменённого наряда
    hand, res = _on_hand("Фильтр (отмена заказа)")
    assert hand == 2 and res == 0
    assert _free("Фильтр (отмена заказа)") == 2


def test_delete_reserved_part_no_500_and_part_added_during_work(client):
    url = _order_to_work(client, "КЕ 1105 КЕ", "0501105000", "Масло (в работе)")
    # Склад: положим ещё масла, чтобы было что резервировать
    _as(client, "oleg")
    # 13.11.2026 (брат): менеджер запчасти не добавляет — только запчастист (во время ремонта — сразу в заказ)
    _as(client, "irina")
    assert client.post(f"{url}/parts", data={"name": "Свеча (в работе)", "qty": "1", "price": "300",
                                             "action": "need"}).status_code == 403
    _as(client, "oleg")
    r = client.post(f"{url}/parts", data={"name": "Свеча (в работе)", "qty": "1", "cost": "150", "action": "order"},
                    follow_redirects=False)
    assert r.status_code == 303
    extra = [p for p in _parts(url) if p.name == "Свеча (в работе)"][0]
    assert extra.status == "ordered"
    # Пока она не выдана — машину не выдать
    _as(client, "irina")
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "fixed", "rate": "100", "qty": "1"})
    _as(client, "andrey")
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    _as(client, "irina")
    from app.services import money as msvc
    with SessionLocal() as db:
        due = msvc.totals(db, _wo(url)).due
    client.post(f"{url}/payments", data={"amount": str(due), "method": "cash", "purpose": "full"})
    assert _wo(url).status == "paid"
    client.post(f"{url}/close", data={"state": "ok"})
    assert _wo(url).status == "paid"   # «Свеча» не выдана со склада

    # Деталь «в резерве» убирается без ошибки сервера, резерв снимается
    _as(client, "irina")
    url2 = _create(client, scenario="A", photos=1, plate="КЕ 1106 КЕ", phone="0501106000", works="ТО").json()["url"]
    _as(client, "oleg")
    client.post(f"{url2}/parts", data={"name": "Масло (в работе)", "qty": "1", "cost": "100", "action": "order"})
    agree(client, url2)
    with SessionLocal() as db:
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == _parts(url2)[0].id))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-1"})
    assert _parts(url2)[0].status == "reserved"
    _as(client, "oleg")
    r = client.post(f"{url2}/parts/{_parts(url2)[0].id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _parts(url2)[0].status == "archived"   # 13.11.2026: «Убрать» — в архив наряда, можно вернуть
    with SessionLocal() as db:
        assert not ssvc.reserved_for_part(db, _parts(url2)[0].id)


def test_payroll_goes_to_new_mechanic_and_second_mechanic_sees_order(client):
    url = _order_to_work(client, "КЕ 1107 КЕ", "0501107000", "Колодки (передача)")
    _as(client, "irina")
    w = _works(url)[0]
    client.post(f"{url}/works/{w.id}", data={"name": w.name, "pricing": "fixed", "rate": "1000", "qty": "1"})
    wo = _wo(url)
    client.post(f"{url}/assign", data={"partsman_id": str(wo.partsman_id), "mechanic_id": str(_uid("sergey")),
                                       "manager_id": str(wo.manager_id), "lift_id": ""})
    assert _works(url)[0].mechanic_id == _uid("sergey")
    # Отдельная работа второму слесарю — он видит наряд и отмечает её
    client.post(f"{url}/works", data={"name": "Развал", "pricing": "fixed", "rate": "500", "qty": "1",
                                      "mechanic_id": str(_uid("andrey"))})
    _as(client, "andrey")
    assert client.get(url).status_code == 200
    w2 = [x for x in _works(url) if x.name == "Развал"][0]
    client.post(f"{url}/works/{w2.id}/state", data={"action": "done"})
    assert [x for x in _works(url) if x.name == "Развал"][0].status == "done"
    _as(client, "sergey")
    client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "1700", "method": "cash", "purpose": "full"})
    assert _wo(url).status == "paid"
    with SessionLocal() as db:
        got = {db.get(User, a.user_id).login: a.amount for a in db.scalars(select(PayrollAccrual).where(
            PayrollAccrual.work_order_id == _wo(url).id, PayrollAccrual.wo_work_id.is_not(None)))}
    assert got == {"sergey": Decimal("300.00"), "andrey": Decimal("150.00")}
    # Мусор в форме назначения — не ошибка сервера
    _as(client, "irina")
    url3 = _create(client, scenario="A", photos=1, plate="КЕ 1108 КЕ", phone="0501108000", works="ТО").json()["url"]
    r = client.post(f"{url3}/assign", data={"partsman_id": "xxx", "mechanic_id": "!!", "lift_id": "zzz", "manager_id": "1"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_reopen_by_request(client):
    url = _ready(client, "КЕ 1109 КЕ", "0501109000", "Колодки (открыть заново)")
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "1200", "method": "cash", "purpose": "full"})
    client.post(f"{url}/close", data={"state": "ok"})
    assert _wo(url).status == "closed"
    client.post(f"{url}/reopen-request", data={"reason": "Клиент вернулся: стук остался"})
    from app.models import OrderReopen
    with SessionLocal() as db:
        r = db.scalar(select(OrderReopen).where(OrderReopen.work_order_id == _wo(url).id))
    assert r.status == "pending"
    # Менеджер сам не открывает
    assert client.post(f"/reopen-requests/{r.id}", data={"action": "approve"}).status_code == 403
    _as(client, "director")
    assert "Открыть наряд заново / ремонт по гарантии · 1" in client.get("/access-requests").text
    client.post(f"/reopen-requests/{r.id}", data={"action": "approve"})
    assert _wo(url).status == "ready"
    # Открыли — можно добавить работу и снова выдать после оплаты
    _as(client, "irina")
    client.post(f"{url}/works", data={"name": "Протяжка ходовой", "pricing": "fixed", "rate": "300", "qty": "1"})
    assert _wo(url).status == "work"


def test_old_debt_can_be_paid(client):
    """Наряд, выданный с долгом до 11.11.2026, — долг можно погасить (раньше оплату не принимало)."""
    url = _ready(client, "КЕ 1117 КЕ", "0501117000", "Колодки (старый долг)")
    _as(client, "irina")
    client.post(f"{url}/payments", data={"amount": "300", "method": "cash", "purpose": "extra"})
    with SessionLocal() as db:   # как было раньше: админ закрыл с долгом
        wo = db.get(WorkOrder, _wo(url).id)
        from app.db import current_branch_id, now as _now
        current_branch_id.set(wo.branch_id)   # прямую запись в журнал — с филиалом (тест не зависит от соседних)
        wo.status, wo.closed_with_debt_comment, wo.closed_at = "closed", "доплатит", _now()
        db.commit()
    assert "Старые долги" in client.get("/money?p=month").text
    page = client.get(url).text
    assert "Принять оплату" in page and "Сохранить скидку" not in page
    client.post(f"{url}/payments", data={"amount": "1000", "method": "cash", "purpose": "full"})   # больше долга — нет
    assert _n_payments(url) == 1
    client.post(f"{url}/payments", data={"amount": "900", "method": "cash", "purpose": "full"})
    assert _n_payments(url) == 2 and _wo(url).status == "closed"


# ---------- Клиенты, машины, поиск ----------
def test_intake_does_not_rename_or_replate(client):
    _as(client, "irina")
    url = _create(client, plate="КЕ 1110 КЕ", phone="0501110000", name="Тест Петров", vin="WVWZZZ1KZAW000111").json()["url"]
    wo = _wo(url)
    # Тот же телефон, другое имя — клиента не переименовываем
    _create(client, plate="КЕ 1110 КЕ", phone="0501110000", name="Совсем другой", mileage="130000")
    with SessionLocal() as db:
        assert db.get(Client, wo.client_id).full_name == "Тест Петров"
    # Выбрали машину и вписали другой номер (VIN другой/пустой) — это другая машина, у старой номер прежний
    r = _create(client, plate="КЕ 1111 КЕ", phone="0501110000", vehicle_id=str(wo.vehicle_id), client_id=str(wo.client_id),
                mileage="1000", vin="")
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(Vehicle, wo.vehicle_id).plate == "КЕ1110КЕ"
    # Пробег меньше прошлого — не принимаем
    r = _create(client, plate="КЕ 1110 КЕ", phone="0501110000", mileage="100")
    assert r.status_code == 400 and "Пробег" in r.text
    # Тот же VIN у нового владельца — та же машина, переходит к нему с историей
    r = _create(client, plate="КЕ 1110 КЕ", phone="0501110999", name="Новый Владелец", vin="WVWZZZ1KZAW000111",
                mileage="140000")
    assert r.status_code == 200
    with SessionLocal() as db:
        v = db.get(Vehicle, wo.vehicle_id)
        new = db.scalar(select(Client).where(Client.phone == "380501110999"))
        assert v.client_id == new.id
        assert db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.vin == "WVWZZZ1KZAW000111")) == 1
    page = client.get(f"/clients/{new.id}").text
    assert wo.number in page and "при прошлом владельце" in page
    # Поиск без учёта регистра по-русски и по VIN в нарядах
    assert "Тест Петров" in client.get("/clients?q=тест").text
    assert wo.number in client.get("/orders?tab=all&q=AW000111").text


def test_intake_twice_same_form_one_order(client):
    _as(client, "irina")
    tok = uuid.uuid4().hex
    a = _create(client, plate="КЕ 1112 КЕ", phone="0501112000", intake_token=tok).json()["url"]
    b = _create(client, plate="КЕ 1112 КЕ", phone="0501112000", intake_token=tok).json()["url"]
    assert a == b


def test_client_and_vehicle_edit(client):
    _as(client, "irina")
    url = _create(client, plate="КЕ 1113 КЕ", phone="0501113000", name="Ира Правка").json()["url"]
    wo = _wo(url)
    client.post(f"/clients/{wo.client_id}/edit", data={"full_name": "Ирина Правка", "phone": "0501113000"})
    client.post(f"/vehicles/{wo.vehicle_id}/edit", data={"make": "Toyota", "model": "Corolla", "plate": "КЕ 1114 КЕ",
                                                        "year": "2019", "vin": "JTDBR32E000000001", "mileage": "125000"})
    with SessionLocal() as db:
        assert db.get(Client, wo.client_id).full_name == "Ирина Правка"
        v = db.get(Vehicle, wo.vehicle_id)
        assert (v.model, v.plate, v.vin) == ("Corolla", "КЕ1114КЕ", "JTDBR32E000000001")
    client.post(f"/vehicles/{wo.vehicle_id}/owner", data={"phone": "0501113999", "name": "Покупатель"})
    with SessionLocal() as db:
        assert db.get(Client, db.get(Vehicle, wo.vehicle_id).client_id).full_name == "Покупатель"


def test_lift_one_car_and_stock_adjust_limits(client):
    _as(client, "irina")
    from app.models import Lift
    from app.services.orders import lift_busy
    with SessionLocal() as db:
        free = next(l for l in db.scalars(select(Lift).order_by(Lift.number.desc())) if not lift_busy(db, l))
        lid = str(free.id)
    assert _create(client, plate="КЕ 1115 КЕ", phone="0501115000", lift_id=lid).status_code == 200
    # Подъёмник занят — машину всё равно принимаем, в очередь (без подъёмника), а не вторую на тот же пост
    r = _create(client, plate="КЕ 1116 КЕ", phone="0501116000", lift_id=lid)
    assert r.status_code == 200
    queued = _wo(r.json()["url"])
    assert queued.lift_id is None
    from app.models import WoStatusHistory
    with SessionLocal() as db:
        assert "в очереди на подъёмник" in db.scalar(select(WoStatusHistory.comment).where(
            WoStatusHistory.work_order_id == queued.id))
    # Поставить на занятый подъёмник из наряда нельзя — сначала освободить
    client.post(f"/orders/{queued.id}/assign", data={"lift_id": lid, "partsman_id": "", "mechanic_id": "",
                                                     "manager_id": str(queued.manager_id)})
    assert _wo(r.json()["url"]).lift_id is None
    # Инвентаризация: минус и ниже резерва — нельзя
    _as(client, "pavel")
    with SessionLocal() as db:
        item = db.scalar(select(StockItem))
    client.post(f"/stock/items/{item.id}/adjust", data={"actual": "-5", "reason": "тест"})
    with SessionLocal() as db:
        b = ssvc.balances(db, item.branch_id, [item.id])[item.id]
    assert b["on_hand"] >= 0


def test_backup_restores(tmp_path):
    from app.services import backup
    p = backup.make("test")
    assert p and p.exists() and backup.check(p)
    import sqlite3
    a = sqlite3.connect(str(p))
    from app.config import DATABASE_URL
    b = sqlite3.connect(DATABASE_URL.removeprefix("sqlite:///"))
    for table in ("work_orders", "payments", "stock_movements", "payroll_accruals"):
        assert a.execute(f"select count(*) from {table}").fetchone() == b.execute(f"select count(*) from {table}").fetchone()
    a.close()
    b.close()
