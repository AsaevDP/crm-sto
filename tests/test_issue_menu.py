"""«Запросы на выдачу» (12.11.2026): отдельный пункт меню у кладовщика с цифрой — сколько запросов ждут."""
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import IssueRequest, PurchaseLine, WoPart

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
    client.post(f"{url}/picking-done")
    with SessionLocal() as db:
        part = db.scalar(select(WoPart).where(WoPart.work_order_id == _wo(url).id))
        pid = db.scalar(select(PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id == part.id))
    client.post(f"/purchases/{pid}", data={"action": "ordered"})
    _as(client, "pavel")
    client.post(f"/purchases/{pid}/receive", data={})
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
