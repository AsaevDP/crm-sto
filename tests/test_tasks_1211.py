"""Задачи от 12.11.2026 (docs/tasks-2026-11-12.md): аванс только директор / зам; «Мне передали» + «Принял»;
черновик закупки не «заказана» и виден кладовщику; Пульт → «Сотрудники» по группам; менеджер видит только своих
клиентов (+3 дня после выдачи) и чужих — по доступу на срок."""
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal, now
from app.models import ClientAccess, PayrollPayout, PurchaseLine, User, WoPart, WorkOrder

from .conftest import login
from .test_order_team import _second_manager
from .test_step3 import _create
from .test_step6 import _as, _order_to_work, _wo


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _n_payouts(uid):
    with SessionLocal() as db:
        return len(db.scalars(select(PayrollPayout).where(PayrollPayout.user_id == uid)).all())


def test_advance_only_director_and_deputy(client):
    andrey = _uid("andrey")
    before = _n_payouts(andrey)
    for who in ("buh", "irina"):
        _as(client, who)
        assert client.post("/payroll/payouts", data={"user_id": str(andrey), "amount": "500", "note": "Аванс"}).status_code == 403
    assert _n_payouts(andrey) == before
    for who in ("director", "deputy"):
        _as(client, who)
        page = client.get(f"/payroll?user={andrey}").text
        assert "Выдать аванс" in page
        client.post("/payroll/payouts", data={"user_id": str(andrey), "amount": "500", "note": "Аванс"})
    assert _n_payouts(andrey) == before + 2
    _as(client, "buh")
    assert "Выдать аванс" not in client.get(f"/payroll?user={andrey}").text


def test_handover_tile_and_accept(client):
    mgr2 = _second_manager()
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КТ 1201 КТ", phone="0501201000").json()["url"]
    wo = _wo(url)
    client.post(f"{url}/assign", data={"partsman_id": str(wo.partsman_id or ""), "mechanic_id": str(wo.mechanic_id or ""),
                                       "lift_id": "", "manager_id": str(mgr2)})
    assert _wo(url).handover_pending
    client.post("/logout")
    client.post("/login", data={"login": "mgr2", "password": "mgr2pass"})
    lst = client.get("/orders?tab=handed").text
    assert "Мне передали" in lst and "КТ 1201 КТ" in lst
    def handed_n():
        with SessionLocal() as db:
            return len(db.scalars(select(WorkOrder).where(WorkOrder.manager_id == mgr2, WorkOrder.handover_pending.is_(True),
                                                          WorkOrder.status.not_in(("closed", "cancel")))).all())
    badges = client.get("/api/notify", headers={"x-requested-with": "fetch"}).json()["badges"]
    assert any("передали" in b["t"] for b in badges.get("/orders", []))
    assert "Вам передали этот наряд" in client.get(url).text
    n_before = handed_n()
    client.post(f"{url}/handover-ack")
    assert not _wo(url).handover_pending and handed_n() == n_before - 1
    assert "КТ 1201 КТ" not in client.get("/orders?tab=handed").text
    client.post("/logout")


def test_draft_purchase_is_not_ordered_and_storekeeper_sees_it(client):
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="КТ 1202 КТ", phone="0501202000", works="ТО").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": "Ремкомплект (черновик)", "qty": "1", "cost": "100", "action": "order"})
    assert "заказать после согласования" in client.get(url).text   # 13.11.2026: до согласования не заказываем
    from .conftest import agree
    agree(client, url)
    page = client.get(url).text
    assert "в черновике закупки" in page
    badges = client.get("/api/notify", headers={"x-requested-with": "fetch"}).json()["badges"]
    assert any("черновики" in b["t"] for b in badges.get("/my-orders", []))
    _as(client, "pavel")
    rec = client.get("/stock/receive").text
    assert "Ещё не заказано у поставщика" in rec and "Ремкомплект (черновик)" in rec
    # Запчастист отправил поставщику — в наряде «заказана», у кладовщика — в «ждём»
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    _as(client, "oleg")
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    assert "в черновике закупки" not in client.get(url).text
    _as(client, "pavel")
    rec = client.get("/stock/receive").text
    drafts_block = rec.split("Ещё не заказано")[1].split("rc-group")[0] if "Ещё не заказано" in rec else ""
    assert "Ремкомплект (черновик)" in rec and "Ремкомплект (черновик)" not in drafts_block


def test_pult_staff_groups(client):
    with SessionLocal() as db:
        oleg = db.scalar(select(User).where(User.login == "oleg"))
        oleg.works_with_id = _uid("irina")
        db.commit()
    _as(client, "irina")
    page = client.get("/pult").text
    assert "Слесари ·" in page and "Кладовщики ·" in page and "Запчастисты ·" in page
    assert "Андрей" in page.split("Слесари ·")[1]
    staff_card = page.split(">Сотрудники<")[1].split("</section>")[0]
    assert "Директоров" not in staff_card and "Бухгалтерова" not in staff_card   # менеджеру — только свои
    _as(client, "director")
    page = client.get("/pult").text
    assert "Менеджеры ·" in page and "Офис ·" in page


def test_manager_sees_only_own_clients_and_access(client):
    _as(client, "irina")
    url = _create(client, plate="КТ 1203 КТ", phone="0501203000", name="Клиент Свойский").json()["url"]
    wo = _wo(url)
    assert "Клиент Свойский" in client.get("/clients").text
    assert client.get(f"/clients/{wo.client_id}").status_code == 200
    # Машину выдали 4 дня назад — у менеджера клиент пропал
    with SessionLocal() as db:
        o = db.get(WorkOrder, wo.id)
        o.status, o.closed_at = "closed", now() - timedelta(days=4)
        db.commit()
        db.execute(WorkOrder.__table__.update().where(WorkOrder.id == wo.id).values(updated_at=now() - timedelta(days=4)))
        db.commit()
    assert "Клиент Свойский" not in client.get("/clients").text
    assert client.get(f"/clients/{wo.client_id}").status_code == 403
    assert client.get(url).status_code == 403
    # В приёмке клиента найти можно
    assert "Клиент Свойский" in client.get("/api/clients/search?q=0501203000").text
    # Запрос доступа → директор даёт на 3 часа → менеджер видит карточку и наряд
    client.post("/clients/access-request", data={"reason": "Клиент Свойский звонит по гарантии"})
    with SessionLocal() as db:
        assert db.scalar(select(ClientAccess).where(ClientAccess.user_id == _uid("irina"), ClientAccess.status == "pending"))
    _as(client, "director")
    assert "Клиент Свойский звонит по гарантии" in client.get("/access-requests").text
    assert "Дать доступ" in client.get(f"/clients/{wo.client_id}").text
    client.post(f"/clients/{wo.client_id}/grant", data={"user_id": str(_uid("irina")), "hours": "3"})
    _as(client, "irina")
    assert client.get(f"/clients/{wo.client_id}").status_code == 200
    assert client.get(url).status_code == 200
    assert "Клиент Свойский" in client.get("/clients").text
    # Директор видит всех клиентов всегда
    _as(client, "director")
    assert "Клиент Свойский" in client.get("/clients").text


def test_returning_client_history_for_new_manager(client):
    """Клиент вернулся через месяц к другому менеджеру — тот видит карточку и ВСЮ историю (прошлый наряд — только смотреть)."""
    _second_manager()
    _as(client, "irina")
    old_url = _create(client, plate="КТ 1204 КТ", phone="0501204000", name="Клиент Вернулся").json()["url"]
    old = _wo(old_url)
    with SessionLocal() as db:
        o = db.get(WorkOrder, old.id)
        o.status, o.closed_at = "closed", now() - timedelta(days=30)
        db.commit()
    client.post("/logout")
    client.post("/login", data={"login": "mgr2", "password": "mgr2pass"})
    new_url = _create(client, plate="КТ 1204 КТ", phone="0501204000", name="Клиент Вернулся").json()["url"]
    new = _wo(new_url)
    assert new.client_id == old.client_id and new.manager_id == _uid("mgr2")
    card = client.get(f"/clients/{new.client_id}").text
    assert old.number in card and new.number in card                      # вся история в карточке
    page = client.get(old_url)
    assert page.status_code == 200 and "Прошлый визит клиента" in page.text
    assert client.post(f"{old_url}/refund", data={"amount": "1", "method": "cash"}).status_code == 403
    assert "Прошлый визит клиента" not in client.get(new_url).text
    # чужие наряды других клиентов по-прежнему закрыты; прежний менеджер клиента больше не видит
    client.post("/logout")
    _as(client, "irina")
    assert client.get(f"/clients/{new.client_id}").status_code == 403
