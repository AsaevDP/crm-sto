"""20.11.2026 (брат, наряд 60): мастер попросил запчасть со склада — у менеджера это было только в «Сообщениях»,
окошко закрыли — и запрос потерялся; слесарь закрыл работы, наряд «Готово», согласовать уже нельзя.

Теперь: 🔴 у «Запросов на выдачу» менеджера + плитка «Согласовать» и карточка с кнопками; плашка в наряде у «Запчастей»;
последнюю работу не закрыть, пока менеджер не решил; застрявший (наряд готов) — только «Отказать».
"""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import IssueRequest, OrderMessage, WorkOrder, WoWork

from .test_messages import _msgs, _stock
from .test_step6 import _as, _order_to_work, _wo


def _red(client, href):
    return sum(x["n"] for x in client.get("/api/notify").json()["badges"].get(href, []) if x["c"] == "red")


def test_ask_visible_to_manager_and_blocks_last_work(client):
    url = _order_to_work(client, "КМ 6060 КМ", "0506060600", "Колодки (видно запрос)")
    item = _stock("Очиститель тормозов (видно запрос)", qty=3)
    _as(client, "irina")
    before = _red(client, "/ready")
    _as(client, "andrey")
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "1",
                                       "text": "свой закончился"})
    m = _msgs(url)[0]
    # мастер видит в наряде, что ждёт менеджера; последнюю работу закрыть нельзя
    page = client.get(url).text
    assert f'id="ask-{m.id}"' in page and "Ждёт решения менеджера" in page
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == _wo(url).id))
    client.post(f"{url}/works/{w.id}/state", data={"action": "start"})
    r = client.post(f"{url}/works/{w.id}/state", data={"action": "done"})
    assert "не решил по запросу со склада" in r.text and _wo(url).status == "work"

    # менеджер: красная цифра, плитка «Согласовать», карточка с кнопками — и в наряде тоже
    _as(client, "irina")
    client.get("/notifications")   # окошко прочитано — цифра всё равно горит (раньше гасла)
    assert _red(client, "/ready") == before + 1
    rp = client.get("/ready").text
    assert "Согласовать" in rp and f'id="ask-{m.id}"' in rp and 'value="approve"' in rp
    assert f'id="ask-{m.id}"' in client.get(url).text
    client.post(f"/messages/{m.id}/decide", data={"action": "approve", "back": "/ready#asks"})
    with SessionLocal() as db:
        assert db.get(OrderMessage, m.id).status == "approved"
        rid = db.scalar(select(IssueRequest.id).where(IssueRequest.work_order_id == _wo(url).id, IssueRequest.status == "new"))
    assert _red(client, "/ready") == before
    _as(client, "pavel")   # база в тестах общая — выдать, чтобы не висел открытый запрос
    client.post(f"/stock/issues/{rid}/issue")


def test_stuck_ask_only_decline(client):
    url = _order_to_work(client, "КМ 6061 КМ", "0506060610", "Колодки (застрял)")
    item = _stock("Очиститель тормозов (застрял)", qty=3)
    _as(client, "andrey")
    client.post("/messages/ask", data={"item_id": str(item), "order_id": str(_wo(url).id), "qty": "1"})
    m = _msgs(url)[0]
    with SessionLocal() as db:   # как в наряде 60: запрос висел, наряд уже «Готово»
        db.get(WorkOrder, _wo(url).id).status = "ready"
        db.commit()
    before_master = client.get("/api/notify").json()["badges"].get("/my-requests", [])
    assert any(x["c"] == "orange" for x in before_master)
    _as(client, "irina")
    rp = client.get("/ready").text
    card = rp[rp.index(f'id="ask-{m.id}"'):][:3000]
    assert "запчасть сюда не добавить" in card and 'value="approve"' not in card
    assert 'value="approve"' not in client.get("/notifications").text.split(f'id="m-{m.id}"')[1][:2500]
    client.post(f"/messages/{m.id}/decide", data={"action": "decline", "answer": "", "back": "/ready"})   # без причины — нет
    with SessionLocal() as db:
        assert db.get(OrderMessage, m.id).status == "pending"
    client.post(f"/messages/{m.id}/decide", data={"action": "decline", "answer": "машина уже готова", "back": "/ready"})
    with SessionLocal() as db:
        assert db.get(OrderMessage, m.id).status == "declined"
    assert f'id="ask-{m.id}"' not in client.get("/ready").text
