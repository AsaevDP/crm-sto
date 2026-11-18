"""17.11.2026 (брат): кладовщик заказывает на склад сам («Закупки» — у каждого свои, у руководства все с фильтром),
«Заказ на склад» на печать, приёмка в «Приходе» (по поставщикам, «Приняты» по накладным) и в «Заказах запчастистов»
(по нарядам, «Мои заказы»), резервная приёмка (зам / директор / менеджер — включается в настройках),
«Всегда в наличии» (норма, сиреневая цифра, «Заказать недостающее»)."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Purchase, PurchaseLine, StockKeep, User

from .test_step6 import _as


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _new_stock_purchase(client, sup, lines):
    pid = uuid.UUID(client.post("/purchases/new", data={"supplier_new": sup}).url.path.rsplit("/", 1)[1])
    for n, q, pr in lines:
        client.post(f"/purchases/{pid}/lines", data={"name": n, "qty": q, "price": pr})
    return pid


def _lines(pid):
    with SessionLocal() as db:
        return {l.id: l for l in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == pid))}


def test_storekeeper_orders_and_receives(client):
    # Кладовщик сам заказывает на склад
    _as(client, "pavel")
    assert "Новая закупка" in client.get("/purchases").text
    pid = _new_stock_purchase(client, "Склад-поставщик 1711", [("Масло 1711", "6", "300"), ("Фильтр 1711", "4", "120")])
    client.post(f"/purchases/{pid}/ordered", data={})
    with SessionLocal() as db:
        p = db.get(Purchase, pid)
        assert p.status == "ordered" and p.created_by == _uid("pavel") and not p.work_order_id
    assert f"/purchases/{pid}" in client.get("/purchases?tab=all").text
    # Документ «Заказ на склад» — без «Утвердил»
    doc = client.get(f"/purchases/{pid}/print").text
    assert "Заказ на склад №" in doc and "Масло 1711" in doc and "Принял на склад" in doc and "Утвердил" not in doc
    # Запчастист чужую закупку в «Закупках» не видит; директор видит всё и может выбрать кладовщика в фильтре
    _as(client, "oleg")
    assert f"/purchases/{pid}" not in client.get("/purchases?tab=all").text
    _as(client, "director")
    page = client.get(f"/purchases?tab=all&who={_uid('pavel')}").text
    assert f"/purchases/{pid}" in page and "кладовщик" in page

    # «Заказы запчастистов» — плитка «Мои заказы» у кладовщика, группа «На склад», приём частями по накладным
    _as(client, "pavel")
    board = client.get("/purchases/by-partsman?u=me").text
    assert "Мои заказы" in board and 'name="grp" value="stock"' in board and "Масло 1711" in board
    ls = {l.catalog_id: l for l in _lines(pid).values()}
    oil = next(l for l in _lines(pid).values() if l.qty == 6)
    # без номера — не принято
    client.post("/stock/receive", data={"line_id": str(oil.id), f"qty_{oil.id}": "2", "back": "/purchases/by-partsman?u=me"})
    assert not _lines(pid)[oil.id].got
    # 2 из 6 по ТТН-А, потом 4 — по ТТН-Б
    client.post("/stock/receive", data={"line_id": str(oil.id), f"qty_{oil.id}": "2", f"inv_{oil.id}": "ТТН-А",
                                        "back": "/purchases/by-partsman?u=me"})
    l = _lines(pid)[oil.id]
    assert l.got == 2 and not l.received and l.invoice == "ТТН-А"
    with SessionLocal() as db:
        assert db.get(Purchase, pid).status == "partial"
    assert "пришло 2 из 6" in client.get("/stock/receive?tab=partial").text
    got = client.get("/stock/receive?tab=got").text
    assert "№ ТТН-А" in got and "Масло 1711" in got
    client.post("/stock/receive", data={"grp": "stock", "who": str(_uid("pavel")), "invoice": "ТТН-Б",
                                        "back": "/purchases/by-partsman?u=me"})
    l = _lines(pid)[oil.id]
    assert l.received and l.invoice == "ТТН-А, ТТН-Б"
    with SessionLocal() as db:
        assert db.get(Purchase, pid).status == "arrived"
    doc = client.get(f"/purchases/{pid}/print").text
    assert "ТТН-А, ТТН-Б" in doc
    got = client.get("/stock/receive?tab=got").text
    assert "№ ТТН-Б" in got and "Фильтр 1711" in got
    assert ls  # noqa
    # 18.11.2026: «Заказы запчастистов → Приняты» — свежие сверху, день по календарику, «Все» сбоку
    from datetime import date, timedelta
    page = client.get("/purchases/by-partsman?st=arrived").text
    assert 'type="date" name="d"' in page and "bp-day-all on" in page and "Масло 1711" in page
    assert "Масло 1711" in client.get(f"/purchases/by-partsman?st=arrived&d={date.today().isoformat()}").text
    old = client.get(f"/purchases/by-partsman?st=arrived&d={(date.today() - timedelta(days=5)).isoformat()}").text
    assert "Масло 1711" not in old and "В этот день ничего нет" in old


def test_arrived_newest_first(client):
    """Два наряда «на склад» нет — проверяем порядок на двух закупках кладовщика: последняя принятая — выше."""
    _as(client, "pavel")
    p1 = _new_stock_purchase(client, "Порядок-1", [("Первая 1811", "1", "10")])
    p2 = _new_stock_purchase(client, "Порядок-2", [("Вторая 1811", "1", "10")])
    for pid in (p1, p2):
        client.post(f"/purchases/{pid}/ordered", data={})
    l1, l2 = next(iter(_lines(p1).values())), next(iter(_lines(p2).values()))
    client.post("/stock/receive", data={"line_id": str(l1.id), f"inv_{l1.id}": "П-1"})
    client.post("/stock/receive", data={"line_id": str(l2.id), f"inv_{l2.id}": "П-2"})
    page = client.get("/purchases/by-partsman?st=arrived&u=me").text
    assert page.index("Вторая 1811") < page.index("Первая 1811")


def test_backup_receiving_by_deputy_director_manager(client):
    _as(client, "pavel")
    pid = _new_stock_purchase(client, "Резерв-поставщик", [("Свеча 1711", "4", "90")])
    client.post(f"/purchases/{pid}/ordered", data={})
    line = next(iter(_lines(pid).values()))
    # По умолчанию зам, директор и менеджер не принимают
    for who in ("deputy", "director", "irina"):
        _as(client, who)
        assert client.get("/stock/receive").status_code == 403, who
    # Включает директор (зам — первым)
    _as(client, "director")
    page = client.get("/settings/receive-backup").text
    assert "Заместитель директора" in page and "Менеджер" in page and "выключено" in page
    client.post("/settings/receive-backup", data={"code": "deputy", "on": "1"})
    _as(client, "deputy")
    assert client.get("/stock/receive").status_code == 200
    client.post("/stock/receive", data={"line_id": str(line.id)})                       # без № накладной — нет
    assert not _lines(pid)[line.id].received
    client.post("/stock/receive", data={"line_id": str(line.id), f"inv_{line.id}": "РЗ-1"})
    assert _lines(pid)[line.id].received and _lines(pid)[line.id].invoice == "РЗ-1"
    # Менеджер — тоже только после включения; выключили зама — снова нельзя
    _as(client, "director")
    client.post("/settings/receive-backup", data={"code": "manager", "on": "1"})
    client.post("/settings/receive-backup", data={"code": "deputy"})
    _as(client, "irina")
    assert client.get("/stock/receive").status_code == 200
    _as(client, "deputy")
    assert client.get("/stock/receive").status_code == 403
    # Кладовщик и мастер настройку не трогают
    _as(client, "pavel")
    assert client.get("/settings/receive-backup").status_code == 403


def test_always_in_stock(client):
    _as(client, "pavel")
    pid = _new_stock_purchase(client, "Норма-поставщик", [("Колодки 1711", "1", "500")])
    client.post(f"/purchases/{pid}/ordered", data={})
    line = next(iter(_lines(pid).values()))
    client.post("/stock/receive", data={"line_id": str(line.id), f"inv_{line.id}": "Н-1"})
    # В список — с нормой 5: свободно 1 → ниже нормы, красная, сиреневая цифра у «Склада»
    client.post("/stock/always", data={"catalog_id": str(line.catalog_id), "min_qty": "5"})
    with SessionLocal() as db:
        k = db.scalar(select(StockKeep).where(StockKeep.catalog_id == line.catalog_id))
        assert k and k.min_qty == 5
    page = client.get("/stock/always").text
    assert "Колодки 1711" in page and "kp-red" in page and "Заказать недостающее" in page
    assert "Всегда в наличии" in client.get("/stock").text
    badges = client.get("/api/notify").json().get("badges", {})
    assert any(b["c"] == "purple" for b in badges.get("/stock", [])), badges.get("/stock")
    # «Заказать недостающее» → черновик на кладовщика, 4 шт (5 − 1), и сразу его страница
    r = client.post("/stock/always/order/missing")
    assert "/purchases/" in str(r.url)
    with SessionLocal() as db:
        d = db.scalar(select(Purchase).where(Purchase.status == "draft", Purchase.created_by == _uid("pavel"),
                                             Purchase.note.like("%Всегда в наличии%")))
        dl = db.scalar(select(PurchaseLine).where(PurchaseLine.purchase_id == d.id))
        assert dl.qty == 4 and dl.catalog_id == line.catalog_id and dl.price == 500
    # Уже в черновике — больше не «не хватает» (жёлтая), повторно заказывать нечего
    page = client.get("/stock/always").text
    assert "kp-yellow" in page and "в черновике 4" in page
    # Ведут список кладовщик, директор, зам; запчастист — нет
    _as(client, "oleg")
    assert client.post("/stock/always", data={"catalog_id": str(line.catalog_id), "min_qty": "9"}).status_code == 403
    _as(client, "deputy")
    assert client.get("/stock/always").status_code == 200
    _as(client, "director")
    assert "Колодки 1711" in client.get("/stock/always/print").text


def test_my_orders_like_storekeeper(client):
    """18.11.2026 (брат): «Мои заказы» у запчастиста — как у кладовщика: группы по нарядам, «Заказать» (черновики,
    поставщик у детали и «Заказал»), свежие сверху, календарик дня и «Все»."""
    from datetime import date, timedelta
    _as(client, "oleg")
    p1 = _new_stock_purchase(client, "Мои-1", [("Старая 1811", "1", "10")])
    p2 = _new_stock_purchase(client, "Мои-2", [("Свежая 1811", "1", "10")])
    page = client.get("/my-orders").text                                    # есть черновики → вкладка «Заказать»
    assert "Заказать" in page and 'data-sup-pick' in page and "Старая 1811" in page
    assert page.index("Свежая 1811") < page.index("Старая 1811")            # свежие сверху
    for pid in (p1, p2):
        client.post(f"/purchases/{pid}/ordered", data={})
    wait = client.get("/my-orders?st=wait").text
    assert "Свежая 1811" in wait and 'type="date" name="d"' in wait and "bp-day-all on" in wait
    assert "Свежая 1811" in client.get(f"/my-orders?st=wait&d={date.today().isoformat()}").text
    assert "Свежая 1811" not in client.get(f"/my-orders?st=wait&d={(date.today() - timedelta(days=3)).isoformat()}").text
    assert 'name="inv_' not in wait                                         # запчастист не принимает
