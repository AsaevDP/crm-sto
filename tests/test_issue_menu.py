"""«Запросы на выдачу» (12.11.2026): отдельный пункт меню у кладовщика с цифрой — сколько запросов ждут."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import IssueRequest, PurchaseLine, WoPart

from .conftest import agree
from .test_step3 import _create
from .test_step6 import _as, _wo


def _sidebar(html: str) -> str:
    return re.search(r'<nav class="sidebar">(.*?)</nav>', html, re.S).group(1)


def test_issue_requests_menu(client):
    _as(client, "pavel")
    side = _sidebar(client.get("/stock/issues").text)
    assert 'href="/stock/issues"' in side and "Запросы на выдачу" in side
    # подсвечен только «Запросы на выдачу», не «Склад»
    assert 'href="/stock/issues" aria-current="page"' in side and 'href="/stock" aria-current' not in side
    before = client.get("/api/notify").json()["issues"]

    # Наряд: запчасть заказали, пришла в резерв → менеджер просит выдать → у кладовщика цифра +1
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="KA2001AA", phone="0670002001", works="Колодки").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": "Колодки (запрос на выдачу)", "qty": "1", "cost": "100", "markup_pct": "50", "action": "order"})
    agree(client, url)   # 13.11.2026: подбор завершён → менеджер согласовал → заказ
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-1"})
    _as(client, "irina")
    client.post(f"{url}/issue-request", data={})
    _as(client, "pavel")
    assert client.get("/api/notify").json()["issues"] == before + 1
    assert "Колодки (запрос на выдачу)" in client.get("/stock/issues").text
    # Выдал → цифра уменьшилась
    with SessionLocal() as db:
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id))
    client.post(f"/stock/issues/{rid}/issue")
    assert client.get("/api/notify").json()["issues"] == before

    # У менеджера и слесаря этого пункта нет
    for who in ("irina", "andrey"):
        _as(client, who)
        assert 'href="/stock/issues"' not in _sidebar(client.get("/orders").text), who


def _reserved_order(client, plate, phone, name):
    """Наряд, где запчасть пришла в резерв и менеджер попросил выдать. Возвращает (url, id запроса)."""
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate=plate, phone=phone, works="Работа").json()["url"]
    _as(client, "oleg")
    client.post(f"{url}/parts", data={"name": name, "qty": "1", "cost": "100", "markup_pct": "50", "action": "order"})
    agree(client, url)   # 13.11.2026: подбор завершён → менеджер согласовал → заказ
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    client.post(f"/purchases/{pid}", data={"action": "ordered", "supplier_new": "Тест-поставщик"})
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={"invoice": "ТН-1"})
    _as(client, "irina")
    client.post(f"{url}/issue-request", data={})
    with SessionLocal() as db:
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id))
    return url, rid


def _status(rid):
    with SessionLocal() as db:
        return db.get(IssueRequest, rid).status


def test_ready_to_issue(client):
    url, rid = _reserved_order(client, "KA2002AA", "0670002002", "Фильтр (готово к выдаче)")
    # Менеджер не может отметить «готово» и не может закрыть ещё не подготовленное
    assert client.post(f"/stock/issues/{rid}/ready").status_code == 403
    assert client.post(f"/stock/issues/{rid}/issue").status_code == 403
    assert _status(rid) == "new"
    menu = _sidebar(client.get("/orders").text)
    assert 'href="/ready"' in menu and "Запросы на выдачу" in menu   # 12.11.2026: так называется у менеджера
    before = client.get("/api/notify").json()["ready"]

    # Кладовщик собрал у окошка → «Готово к выдаче»; у менеджера загорается + сообщение
    _as(client, "pavel")
    page = client.get("/stock/issues").text
    assert "📦 Готово к выдаче" in page and "Выдано сразу" in page
    assert 'href="/ready"' not in _sidebar(page)          # у кладовщика это в «Запросах на выдачу»
    client.post(f"/stock/issues/{rid}/ready")
    assert _status(rid) == "ready"
    assert "Готово — ждут, когда заберут · " in client.get("/stock/issues").text
    _as(client, "irina")
    j = client.get("/api/notify").json()
    assert j["ready"] == before + 1 and any(n["kind"] == "issue_ready" for n in j["notes"])
    ready = client.get("/ready").text
    assert "Фильтр (готово к выдаче)" in ready and "у окошка кладовщика" in ready
    order = client.get(url).text
    assert "Запчасти готовы к выдаче" in order
    # Менеджер забрал → «Выдано» → наряд в работе, цифра ушла, в ленте видно
    r = client.post(f"/stock/issues/{rid}/issue", data={"back": "/ready"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ready"
    assert _status(rid) == "issued" and _wo(url).status == "work"
    assert client.get("/api/notify").json()["ready"] == before
    assert "подготовил запчасти — готово к выдаче" in client.get(url).text

    # Второй наряд: «готово», а закрывает кладовщик — тоже можно
    url2, rid2 = _reserved_order(client, "KA2003AA", "0670002003", "Свеча (готово к выдаче)")
    _as(client, "pavel")
    client.post(f"/stock/issues/{rid2}/ready")
    client.post(f"/stock/issues/{rid2}/issue")
    assert _status(rid2) == "issued"

    # Слесарь не может ни подготовить, ни закрыть
    url3, rid3 = _reserved_order(client, "KA2004AA", "0670002004", "Датчик (готово к выдаче)")
    _as(client, "pavel")
    client.post(f"/stock/issues/{rid3}/ready")
    _as(client, "andrey")
    assert client.post(f"/stock/issues/{rid3}/issue").status_code == 403
    assert client.get("/ready").status_code == 403
