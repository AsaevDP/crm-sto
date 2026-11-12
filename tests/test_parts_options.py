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
    login(client, "irina", "irina123")
    names = client.get("/api/brands").json()["items"]
    assert "Bosch" in names and "TRW" in names                      # стартовый список
    assert client.post("/api/brands", data={"name": "  Пример-Бренд  "}, headers=FETCH).json()["name"] == "Пример-Бренд"
    client.post("/api/brands", data={"name": "пример-бренд"}, headers=FETCH)          # без дубля
    client.post("/logout")
    login(client, "oleg", "oleg123")
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

    # Сразу при согласовании: три варианта колодок разных фирм + обычная позиция «в наряд»
    client.post(f"{url}/parts", data={"name": "Колодки передние (тест вариантов)", "brand": "TRW", "price": "1100",
                                      "action": "option"})
    grp = next(iter(_parts(url).values())).option_group
    for brand, price in (("Bosch", "950"), ("Оригинал", "2400")):
        client.post(f"{url}/parts", data={"name": "Колодки передние (тест вариантов)", "brand": brand, "price": price,
                                          "action": "option", "option_group": grp})
    client.post(f"{url}/parts", data={"name": "Датчик износа колодок (тест)", "brand": "Bosch", "price": "300",
                                      "action": "auto"})           # менеджер не заказывает — только «нужна»
    ps = _parts(url)
    assert sorted(p.status for p in ps.values()) == ["need", "option", "option", "option"]
    assert len({p.option_group for p in ps.values() if p.option_group}) == 1
    from app.models import PartCatalog
    with SessionLocal() as db:   # одна деталь — три разные фирмы в каталоге
        brands = {db.get(PartCatalog, p.catalog_id).brand for p in ps.values() if p.option_group}
    assert brands == {"TRW", "Bosch", "Оригинал"}
    assert not any(p.picked_by for p in ps.values())
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).parts == Decimal("300.00")   # варианты не в счёте
    assert "выбор из 3" in client.get(url).text

    # Клиент выбрал Bosch: он «нужна», остальные «не выбран»
    bosch = next(p for p in ps.values() if p.price == Decimal("950.00"))
    client.post(f"{url}/parts/{bosch.id}/choose")
    st = {p.price: p.status for p in _parts(url).values() if p.option_group}
    assert st == {Decimal("950.00"): "need", Decimal("1100.00"): "declined", Decimal("2400.00"): "declined"}
    with SessionLocal() as db:
        assert msvc.totals(db, _wo(url)).parts == Decimal("1250.00")

    # Согласование → подбор: запчастист берёт «нужна» — заказывает
    f_id = None
    from app.models import WoFinding
    with SessionLocal() as db:
        f_id = db.scalar(select(WoFinding.id).where(WoFinding.work_order_id == _wo(url).id))
    client.post(f"{url}/findings/{f_id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/approval-done")
    assert _wo(url).status == "picking"
    client.post("/logout")
    login(client, "oleg", "oleg123")
    assert "Со склада / заказать" in client.get(url).text
    # Закупку вписал запчастист — цена, которую менеджер назвал клиенту, не меняется, наценка считается сама
    sensor = next(p for p in _parts(url).values() if p.price == Decimal("300.00"))
    r = client.post(f"{url}/parts/{sensor.id}/markup", data={"cost": "200", "markup_pct": "0"}, headers=FETCH).json()
    assert (r["price"], r["markup"]) == ("300.00", 50)
    r = client.post(f"{url}/parts/{sensor.id}/markup", data={"cost": "200", "markup_pct": "60"}, headers=FETCH).json()
    assert r["price"] == "320.00"                                    # дальше наценка — как обычно
    client.post(f"{url}/parts/{sensor.id}/markup", data={"cost": "200", "markup_pct": "50"}, headers=FETCH)
    client.post(f"{url}/picking-done")
    assert _wo(url).status == "picking"                              # ещё есть «нужна»
    for p in _parts(url).values():
        if p.status == "need":
            client.post(f"{url}/parts/{p.id}/source", data={"action": "order"})
    with SessionLocal() as db:
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id.in_(list(_parts(url))))).all()
        assert len(lines) == 2                                       # заказаны только выбранный вариант и датчик
    client.post(f"{url}/picking-done")
    assert _wo(url).status == "waiting"
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


def test_options_block_picking_until_client_chooses(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 7002 КА", phone="0507002000").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    assert _wo(url).status == "picking"
    client.post(f"{url}/parts", data={"name": "Амортизатор (тест)", "brand": "Kayaba", "cost": "1000", "markup_pct": "30",
                                      "action": "option"})
    client.post(f"{url}/picking-done")
    assert _wo(url).status == "picking"                              # клиент ещё не выбрал
    p = next(iter(_parts(url).values()))
    assert p.price == Decimal("1300.00") and p.status == "option"
    client.post(f"{url}/parts/{p.id}/choose")                       # выбор в подборе — сразу в закупку
    assert _parts(url)[p.id].status == "ordered"
    client.post(f"{url}/picking-done")
    assert _wo(url).status == "waiting"


def test_manager_sends_to_diagnosis(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate="КА 7003 КА", phone="0507003000", mechanic_id="").json()["url"]
    from app.models import User
    with SessionLocal() as db:
        sid = db.scalar(select(User.id).where(User.login == "sergey"))
    client.post(f"{url}/to-diag", data={"mechanic_id": str(sid)})
    wo = _wo(url)
    assert wo.status == "diag" and wo.mechanic_id == sid
