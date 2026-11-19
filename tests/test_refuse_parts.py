"""Отказ клиента и варианты (19.11.2026, брат; разбор наряда 39 — три комплекта передних колодок):
- до согласования «Убрать» — без причины; после — «Клиент отказался» с причиной;
- уже заказанное у поставщика → «Лишнее после отказов»: запчастист «Отменил у поставщика» / «Пусть приедет — на склад»;
  приехавшее → «Оставить», «Вернуть поставщику» (списание), «Продать»; клиент передумал — та же строка заказа снова в наряде;
- деталь со склада тоже может быть вариантом на выбор; вариант без пары не проходит «Подбор завершён»;
- одна деталь несколько раз — «Согласовал с клиентом» только с галочкой «Да, клиент берёт все»."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Location, PartCatalog, PartLeftover, Purchase, PurchaseLine, StockItem, WoPart, WorkOrder
from app.services import stock as ssvc

from .conftest import agree, login
from .test_step3 import _create


def _parts(url):
    with SessionLocal() as db:
        return db.scalars(select(WoPart).where(WoPart.work_order_id == uuid.UUID(url.rsplit("/", 1)[1]))
                          .order_by(WoPart.created_at)).all()


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _free(cat_id):
    with SessionLocal() as db:
        items = db.scalars(select(StockItem).where(StockItem.catalog_id == cat_id)).all()
        if not items:
            return Decimal(0)
        return sum((b["free"] for b in ssvc.balances(db, items[0].branch_id, [i.id for i in items]).values()), Decimal(0))


def _stock_item(client, name, qty="3"):
    """Положить деталь на склад (закупка кладовщика)."""
    login(client, "pavel", "pavel123")
    r = client.post("/purchases/new", data={"supplier_new": "Склад-тест"})
    pid = uuid.UUID(r.url.path.rsplit("/", 1)[1])
    client.post(f"/purchases/{pid}/lines", data={"name": name, "qty": qty, "price": "400"})
    client.post(f"/purchases/{pid}/ordered", data={})
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-ст", "all": str(pid)})
    client.post("/logout")
    with SessionLocal() as db:
        return db.scalar(select(PartCatalog).where(PartCatalog.name == name))


def _new_order(client, plate, phone):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone).json()["url"]
    client.post("/logout")
    return url


def test_stock_part_as_variant_and_lonely_variant_and_duplicates(client):
    cat = _stock_item(client, "Колодки передние (вариант-склад)")
    url = _new_order(client, "КА 3901 КА", "0503901000")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"catalog_id": str(cat.id), "name": cat.name, "qty": "1", "cost": "400",
                                      "markup_pct": "30", "action": "auto"})
    base = _parts(url)[0]
    assert base.status == "reserved" and _free(cat.id) == 2
    # варианту без пары «Подбор завершён» не даём
    client.post(f"{url}/parts", data={"name": "Свеча (одинокий вариант)", "brand": "NGK", "qty": "1", "cost": "100",
                                      "markup_pct": "30", "action": "option"})
    client.post(f"{url}/picking-done")
    assert _wo(url).picked_at is None
    lonely = next(p for p in _parts(url) if p.name.startswith("Свеча"))
    client.post(f"{url}/parts/{lonely.id}/delete")   # до согласования — «Убрать» без причины
    assert next(p for p in _parts(url) if p.id == lonely.id).status == "archived"
    # вариант к детали со склада: она тоже становится вариантом, резерв снимается до выбора клиента
    client.post(f"{url}/parts", data={"name": "Колодки передние (вариант-склад)", "brand": "Jurid", "qty": "1",
                                      "cost": "700", "markup_pct": "30", "action": "option", "option_group": f"part:{base.id}"})
    ps = [p for p in _parts(url) if p.status != "archived"]
    assert len({p.option_group for p in ps}) == 1 and all(p.status == "option" for p in ps)
    assert _free(cat.id) == 3
    client.post(f"{url}/picking-done")
    assert _wo(url).picked_at is not None
    client.post("/logout")
    # менеджер: клиент выбрал Jurid — TRW со склада свободна, в счёт не идёт
    login(client, "irina", "irina123")
    jurid = next(p for p in ps if p.id != base.id)
    client.post(f"{url}/parts/{jurid.id}/choose")
    assert {p.id: p.status for p in _parts(url)}[base.id] == "declined" and _free(cat.id) == 3
    client.post("/logout")

    # дубли: ещё одна такая же деталь отдельной позицией → без галочки не согласовать
    url2 = _new_order(client, "КА 3902 КА", "0503902000")
    login(client, "oleg", "oleg123")
    for brand in ("TRW", "Toyota"):
        client.post(f"{url2}/parts", data={"name": "Диск тормозной (дубль)", "brand": brand, "qty": "1", "cost": "900",
                                           "markup_pct": "30", "action": "order"})
    client.post(f"{url2}/picking-done")
    client.post("/logout")
    login(client, "irina", "irina123")
    page = " ".join(client.get(url2).text.split())
    assert "Да, клиент берёт все" in page and "«Диск тормозной (дубль)» — 2 позиции" in page
    r = client.post(f"{url2}/approval-done")
    assert _wo(url2).status == "approve" and "клиент берёт все" in r.text
    client.post(f"{url2}/approval-done", data={"dups_ok": "1"})
    assert _wo(url2).status != "approve"
    client.post("/logout")


def test_refuse_after_approval_ordered_and_arrived(client):
    url = _new_order(client, "КА 3903 КА", "0503903000")
    login(client, "oleg", "oleg123")
    for name in ("Рычаг (отказ — отменят)", "Шаровая (отказ — приедет)", "Пыльник (отказ — передумал)"):
        client.post(f"{url}/parts", data={"name": name, "qty": "1", "cost": "500", "markup_pct": "30", "action": "order"})
    agree(client, url, then="oleg")
    with SessionLocal() as db:
        pids = {l.purchase_id for p in _parts(url) for l in db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id == p.id))}
    for pid in pids:
        client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Поставщик-отказ"})
    client.post("/logout")
    lever, ball, boot = _parts(url)
    assert {p.status for p in (lever, ball, boot)} == {"ordered"}

    # менеджер: после согласования — только «Клиент отказался» с причиной
    login(client, "irina", "irina123")
    assert "Оформить отказ клиента" in client.get(url).text
    client.post(f"{url}/parts/{lever.id}/delete")
    assert next(p for p in _parts(url) if p.id == lever.id).status == "ordered"
    for p in (lever, ball, boot):
        client.post(f"{url}/parts/{p.id}/delete", data={"reason": "Клиент купил сам"})
    assert all(p.status == "archived" and p.refuse_reason == "Клиент купил сам" for p in _parts(url))
    # передумал по пыльнику — тот же заказ снова под машину (второй раз не заказываем)
    client.post(f"{url}/parts/{boot.id}/restore")
    with SessionLocal() as db:
        b = db.get(WoPart, boot.id)
        assert b.status == "ordered" and db.scalar(select(PurchaseLine).where(PurchaseLine.wo_part_id == boot.id))
        assert len(db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id == boot.id)).all()) == 1
    client.post("/logout")

    # запчастист: красная цифра, «Лишнее после отказов»
    login(client, "oleg", "oleg123")
    b = client.get("/api/notify").json()["badges"].get("/my-orders", [])
    assert any(x["c"] == "red" and "клиент отказался" in x["t"] for x in b)
    page = client.get("/stock/leftovers").text
    assert "Рычаг (отказ — отменят)" in page and "Отменил у поставщика" in page
    with SessionLocal() as db:
        lo_lever = db.scalar(select(PartLeftover).where(PartLeftover.wo_part_id == lever.id))
        lo_ball = db.scalar(select(PartLeftover).where(PartLeftover.wo_part_id == ball.id))
        lever_line = lo_lever.purchase_line_id
    assert lo_lever.status == lo_ball.status == "decide"
    client.post(f"/stock/leftovers/{lo_lever.id}", data={"action": "cancel"})
    client.post(f"/stock/leftovers/{lo_ball.id}", data={"action": "to_stock", "note": "Поставщик не принимает отказ"})
    with SessionLocal() as db:
        assert db.get(PartLeftover, lo_lever.id).status == "cancelled" and db.get(PurchaseLine, lever_line) is None
        assert db.get(PartLeftover, lo_ball.id).status == "wait"
        ball_line = db.get(PurchaseLine, lo_ball.purchase_line_id)
        assert ball_line.wo_part_id is None
    client.post("/logout")

    # шаровая приехала — ляжет на склад свободной; запчастист: «Вернуть поставщику» — списание со склада
    login(client, "pavel", "pavel123")
    client.post("/stock/locations", data={"rack": "R", "shelves": "1", "places": "1"})
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "R"))
    client.post("/stock/receive", data={"invoice": "ТН-отказ", "line_id": str(ball_line.id), f"loc_{ball_line.id}": str(loc.id)})
    client.post("/logout")
    with SessionLocal() as db:
        cat_id = db.get(WoPart, ball.id).catalog_id
    assert _free(cat_id) == 1
    login(client, "oleg", "oleg123")
    assert "на складе — решить" in client.get("/stock/leftovers").text
    client.post(f"/stock/leftovers/{lo_ball.id}", data={"action": "sale"})
    client.post(f"/stock/leftovers/{lo_ball.id}", data={"action": "return"})
    with SessionLocal() as db:
        assert db.get(PartLeftover, lo_ball.id).status == "returned"
    assert _free(cat_id) == 0
    client.post("/logout")
    # менеджер видит в архиве наряда причину и решение
    login(client, "irina", "irina123")
    page = client.get(url).text
    assert "Причина: Клиент купил сам" in page and "вернули поставщику" in page
    client.post("/logout")


def test_refuse_reserved_from_shelf_just_returns_to_shelf(client):
    cat = _stock_item(client, "Фильтр (с полки, отказ)", qty="2")
    url = _new_order(client, "КА 3904 КА", "0503904000")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"catalog_id": str(cat.id), "name": cat.name, "qty": "1", "cost": "400",
                                      "markup_pct": "30", "action": "auto"})
    agree(client, url, then="irina")
    p = _parts(url)[0]
    assert p.status == "reserved" and _free(cat.id) == 1
    client.post(f"{url}/parts/{p.id}/delete", data={"reason": "Поменяли в прошлый раз"})
    client.post("/logout")
    assert _free(cat.id) == 2
    with SessionLocal() as db:
        assert db.scalar(select(PartLeftover).where(PartLeftover.wo_part_id == p.id)) is None   # не «лишнее» — своя полка


def test_refuse_part_in_issue_request_tells_mechanic_and_storekeeper(client):
    """19.11.2026 (брат, наряд 61): слесарь попросил выдать рычаг, клиент отказался — деталь уходит из запроса
    (пустой запрос закрывается), слесарю «не ставить», кладовщику «не выдавать»; запчастист отказ не оформляет."""
    from app.db import current_branch_id
    from app.models import IssueRequest, Notification, User
    url = _new_order(client, "КА 6161 КА", "0506161000")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Рычаг (наряд 61)", "qty": "1", "cost": "500", "markup_pct": "30", "action": "order"})
    agree(client, url, then=None)
    oid = _wo(url).id
    with SessionLocal() as db:   # деталь приехала под машину, наряд «Запчасти на складе», слесарь — Андрей
        wo = db.get(WorkOrder, oid)
        current_branch_id.set(wo.branch_id)
        wo.mechanic_id = db.scalar(select(User.id).where(User.login == "andrey"))
        p = db.scalars(select(WoPart).where(WoPart.work_order_id == oid)).first()
        p.status, p.source, wo.status = "reserved", "purchased", "instock"
        db.commit()
        pid = p.id
    login(client, "andrey", "andrey123")
    client.post(f"{url}/issue-request", data={})
    client.post("/logout")
    with SessionLocal() as db:
        req = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == oid))
    assert req.status == "new"
    login(client, "oleg", "oleg123")   # запчастист — нельзя
    client.post(f"{url}/parts/{pid}/delete", data={"reason": "купил сам"})
    client.post("/logout")
    assert next(x for x in _parts(url) if x.id == pid).status == "reserved"
    login(client, "irina", "irina123")
    client.post(f"{url}/parts/{pid}/delete", data={"reason": "купил сам"})
    client.post("/logout")
    with SessionLocal() as db:
        assert db.get(WoPart, pid).status == "archived" and db.get(IssueRequest, req.id).status == "cancelled"
        who = {db.get(User, n.user_id).login for n in db.scalars(select(Notification).where(
            Notification.work_order_id == oid, Notification.kind == "refused_info"))}
    assert "andrey" in who and "pavel" in who