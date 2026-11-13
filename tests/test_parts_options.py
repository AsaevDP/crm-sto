"""Наряд 11.11.2026: бренды из общего списка, варианты запчастей на выбор клиенту, менеджер пишет проблемы и
вносит запчасти сам, шаги наряда, акт запчастей и акт выполненных работ."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Brand, PurchaseLine, WoPart, WorkOrder
from app.services import money as msvc

from .conftest import login
from .test_step3 import _create
from .test_step4 import FETCH, _finding


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _parts(url):
    with SessionLocal() as db:
        return {p.id: p for p in db.scalars(select(WoPart).where(WoPart.work_order_id == _wo(url).id))}


def test_brands_list_shared_and_added_from_order(client):
    # 13.11.2026: запчасти (и бренды) добавляет запчастист; менеджер список видит
    login(client, "oleg", "oleg123")
    names = client.get("/api/brands").json()["items"]
    assert "Bosch" in names and "TRW" in names                      # стартовый список
    assert client.post("/api/brands", data={"name": "  Пример-Бренд  "}, headers=FETCH).json()["name"] == "Пример-Бренд"
    client.post("/api/brands", data={"name": "пример-бренд"}, headers=FETCH)          # без дубля
    client.post("/logout")
    login(client, "irina", "irina123")
    assert client.get("/api/brands").json()["items"].count("Пример-Бренд") == 1      # видят все
    client.post("/logout")
    login(client, "admin", "admin123")
    page = client.get("/settings/brands").text
    assert "Пример-Бренд" in page and "Бренды запчастей" in client.get("/settings").text
    with SessionLocal() as db:
        b = db.scalar(select(Brand).where(Brand.name == "Пример-Бренд"))
    client.post("/settings/brands", data={"action": "rename", "bid": str(b.id), "name": "Пример Бренд 2"})
    assert "Пример Бренд 2" in client.get("/api/brands").json()["items"]


def test_manager_problems_options_choice_and_acts(client):
    # Сценарий Б без слесаря → «Создан»: менеджер пишет проблемы со слов клиента
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 7001 КА", phone="0507001000", mechanic_id="").json()["url"]
    assert _wo(url).status == "created"
    page = client.get(url).text
    assert "wo-steps" in page and "Что с машиной?" in page and "На диагностику" in page
    assert _finding(client, url, "Скрипят передние тормоза", "urgent").json()["ok"]
    client.post(f"{url}/to-approve")
    assert _wo(url).status == "approve"
    # 13.11.2026 (брат): менеджер запчасти не добавляет — у него нет формы, сервер тоже не даст
    assert "Добавить запчасть" not in client.get(url).text
    assert client.post(f"{url}/parts", data={"name": "X", "price": "1", "action": "need"}).status_code == 403
    client.post("/logout")

    # Запчастист на согласовании: три варианта колодок разных фирм (одна группа) + датчик «заказать»
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Колодки передние (тест вариантов)", "brand": "TRW", "cost": "1000",
                                      "markup_pct": "10", "action": "option"})
    grp = next(iter(_parts(url).values())).option_group
    assert grp in client.get(url).text                               # «ещё вариант к: …» в списке формы
    for brand, cost in (("Bosch", "950"), ("Оригинал", "2400")):
        client.post(f"{url}/parts", data={"name": "Колодки передние (тест вариантов)", "brand": brand, "cost": cost,
                                          "markup_pct": "0", "action": "option", "option_group": grp})
    client.post(f"{url}/parts", data={"name": "Датчик износа колодок (тест)", "brand": "Bosch", "cost": "300",
                                      "markup_pct": "0", "action": "order"})
    ps = _parts(url)
    assert sorted(p.status for p in ps.values()) == ["need", "option", "option", "option"]
    assert len({p.option_group for p in ps.values() if p.option_group}) == 1
    from app.models import PartCatalog
    with SessionLocal() as db:   # одна деталь — три разные фирмы в каталоге
        brands = {db.get(PartCatalog, p.catalog_id).brand for p in ps.values() if p.option_group}
        assert not db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id.in_(list(ps)))).all()   # не заказано
    assert brands == {"TRW", "Bosch", "Оригинал"}
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).parts == Decimal("300.00")   # варианты не в счёте
    assert "выбор из 3" in client.get(url).text
    client.post(f"{url}/picking-done")                               # варианты — выбирает клиент через менеджера
    assert _wo(url).picked_at is not None and _wo(url).status == "approve"
    client.post("/logout")

    # Менеджер: клиент выбрал Bosch — «нужна», остальные «не выбран»
    login(client, "irina", "irina123")
    bosch = next(p for p in ps.values() if p.price == Decimal("950.00"))
    client.post(f"{url}/parts/{bosch.id}/choose")
    st = {p.price: p.status for p in _parts(url).values() if p.option_group}
    assert st == {Decimal("950.00"): "need", Decimal("1100.00"): "declined", Decimal("2400.00"): "declined"}
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).parts == Decimal("1250.00")
    # Ошибся / клиент передумал: убрал датчик в архив и вернул
    sensor = next(p for p in _parts(url).values() if p.price == Decimal("300.00"))
    client.post(f"{url}/parts/{sensor.id}/delete")
    assert _parts(url)[sensor.id].status == "archived"
    page = client.get(url).text
    assert "Архив · 1" in page and f"/parts/{sensor.id}/restore" in page
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).parts == Decimal("950.00")
    client.post(f"{url}/parts/{sensor.id}/restore")
    assert (_parts(url)[sensor.id].status, _parts(url)[sensor.id].plan) == ("need", "order")

    # «Согласовал с клиентом» → только теперь заказ (оба — в черновик запчастиста) и сообщение ему
    from app.models import Notification, User, WoFinding
    with SessionLocal() as db:
        f_id = db.scalar(select(WoFinding.id).where(WoFinding.work_order_id == _wo(url).id))
    client.post(f"{url}/findings/{f_id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "waiting"
    with SessionLocal() as db:
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id.in_(list(_parts(url))))).all()
        assert len(lines) == 2                                       # заказаны только выбранный вариант и датчик
        oleg = db.scalar(select(User.id).where(User.login == "oleg"))
        from app.models import Purchase
        assert {db.get(Purchase, l.purchase_id).created_by for l in lines} == {oleg}
        assert db.scalar(select(Notification).where(Notification.user_id == oleg, Notification.kind == "to_order",
                                                    Notification.work_order_id == _wo(url).id))
    client.post("/logout")

    # «Мои заказы»: заказать по наряду — по строке «Заказал», последняя строка — вся закупка «заказано»
    login(client, "oleg", "oleg123")
    mine = client.get("/my-orders").text
    assert "Заказать по нарядам" in mine and _wo(url).number in mine
    # 13.11.2026: без поставщика «Заказал» нельзя; заказанная деталь сразу в накладной у кладовщика
    client.post(f"/purchase-lines/{lines[0].id}/ordered", data={"back": "/my-orders"})
    with SessionLocal() as db:
        assert not db.get(PurchaseLine, lines[0].id).ordered_at
    client.post(f"/purchase-lines/{lines[0].id}/supplier", data={"supplier_new": "Опции-поставщик"})
    client.post(f"/purchase-lines/{lines[0].id}/ordered", data={"back": "/my-orders"})
    with SessionLocal() as db:
        l0 = db.get(PurchaseLine, lines[0].id)
        assert l0.ordered_at and db.get(Purchase, l0.purchase_id).status == "ordered"
        assert db.get(Purchase, db.get(PurchaseLine, lines[1].id).purchase_id).status == "draft"
    client.post(f"/purchase-lines/{lines[1].id}/supplier", data={"supplier_new": "Опции-поставщик"})
    client.post(f"/purchase-lines/{lines[1].id}/ordered", data={"back": "/my-orders"})
    with SessionLocal() as db:
        l1 = db.get(PurchaseLine, lines[1].id)
        assert l1.purchase_id == db.get(PurchaseLine, lines[0].id).purchase_id   # тот же поставщик — та же накладная
    client.post("/logout")

    # 12.11.2026: документов три — запчасти (только выбранное, с брендом, без цен) и работы теперь в Акте выдачи;
    # старые адреса актов ведут туда
    login(client, "irina", "irina123")
    act = client.get(f"{url}/act/parts").text
    assert "АКТ ВЫДАЧИ" in act and "Bosch" in act and "2400" not in act
    assert "Скрипят передние тормоза" in client.get(f"{url}/act/works").text
    assert "Снятые (старые) запчасти" in act and "месяцев" not in act and "Гарантия на работы" not in act
    assert "претензий по объёму, качеству и срокам не имеет" in act
    assert "Гарантия не распространяется на запчасти заказчика" in act
    page = client.get(url).text
    assert "Акт запчастей" not in page and "Акт выполненных работ" not in page and "Акт приёма" in page


def test_options_block_approval_until_client_chooses(client):
    """13.11.2026: запчастист может закончить подбор с вариантами — выбирает клиент через менеджера; согласовать
    с клиентом нельзя, пока вариант не выбран."""
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 7002 КА", phone="0507002000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    assert _wo(url).status == "approve"
    client.post(f"{url}/parts", data={"name": "Амортизатор (тест)", "brand": "Kayaba", "cost": "1000", "markup_pct": "30",
                                      "action": "option"})
    client.post(f"{url}/picking-done")
    assert _wo(url).picked_at is not None
    p = next(iter(_parts(url).values()))
    assert p.price == Decimal("1300.00") and p.status == "option"
    client.post("/logout")
    login(client, "irina", "irina123")
    assert "ещё не выбрал вариант" in client.post(f"{url}/approval-done").text
    assert _wo(url).status == "approve"
    client.post(f"{url}/parts/{p.id}/choose")                       # до согласования — не заказано
    assert (_parts(url)[p.id].status, _parts(url)[p.id].plan) == ("need", "order")
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "waiting" and _parts(url)[p.id].status == "ordered"


def test_manager_sends_to_diagnosis(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 7003 КА", phone="0507003000", mechanic_id="").json()["url"]
    from app.models import User
    with SessionLocal() as db:
        sid = db.scalar(select(User.id).where(User.login == "sergey"))
    client.post(f"{url}/to-diag", data={"mechanic_id": str(sid)})
    wo = _wo(url)
    assert wo.status == "diag" and wo.mechanic_id == sid
