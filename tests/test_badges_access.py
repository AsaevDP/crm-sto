"""12.11.2026: цветные цифры в меню; менеджер видит только свои наряды; доступ ко всем — по запросу на срок
(админ / директор / зам); перестановка пар «менеджер ↔ запчастист»."""
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, OrderAccess, User, WorkOrder

from .conftest import login
from .test_order_team import _second_manager
from .test_step3 import _create
from .test_step6 import _as, _wo


def _uid(login_):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.login == login_))


def _badges(client, href):
    return {b["c"]: b["n"] for b in client.get("/api/notify").json()["badges"].get(href, [])}


def test_manager_sees_own_orders_and_asks_access(client):
    mgr2 = _second_manager()
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="KA3001AA", phone="0670003001", works="Своя работа").json()["url"]
    client.post("/logout")
    login(client, "mgr2", "mgr2pass")
    assert client.get(url).status_code == 403                       # чужой наряд не открыть
    assert _wo(url).number not in client.get("/orders?tab=all").text
    page = client.get("/orders").text
    assert "Попросить доступ ко всем нарядам" in page
    # Запрос доступа → админу / директору / заму сообщение и красная цифра
    client.post("/orders/access-request", data={"reason": "Ирина на больничном"})
    assert "Запрос на доступ ко всем нарядам отправлен" in client.get("/orders").text
    with SessionLocal() as db:
        a = db.scalar(select(OrderAccess).where(OrderAccess.user_id == mgr2, OrderAccess.status == "pending"))
        assert a and a.reason == "Ирина на больничном"
        for who in ("admin", "director"):
            assert db.scalar(select(Notification).where(Notification.user_id == _uid(who), Notification.kind == "access_req"))
    client.post("/logout")
    _as(client, "director")
    assert _badges(client, "/orders").get("red", 0) >= 1
    page = client.get("/access-requests").text
    assert "Ирина на больничном" in page and "неделю" in page
    client.post(f"/access-requests/{a.id}", data={"action": "approve", "hours": "2"})
    with SessionLocal() as db:
        a2 = db.get(OrderAccess, a.id)
        until = a2.until if a2.until.tzinfo else a2.until.replace(tzinfo=timezone.utc)
        assert a2.status == "approved" and a2.hours == 2 and 1.9 * 3600 < (until - datetime.now(timezone.utc)).total_seconds() < 2.1 * 3600
    client.post("/logout")
    login(client, "mgr2", "mgr2pass")
    assert client.get(url).status_code == 200                       # теперь видит все
    assert "Доступ ко <b>всем нарядам</b> до" in client.get("/orders").text
    client.post("/logout")
    # Админ закрыл доступ досрочно → снова только свои
    _as(client, "admin")
    client.post(f"/access-requests/{a.id}", data={"action": "close"})
    client.post("/logout")
    login(client, "mgr2", "mgr2pass")
    assert client.get(url).status_code == 403
    client.post("/logout")
    # Менеджер не может сам разрешать
    _as(client, "irina")
    assert client.get("/access-requests").status_code == 403


def test_menu_badges_colors(client):
    # Менеджер: наряд «готов к выдаче» — зелёная цифра в «Нарядах»
    _as(client, "irina")
    url = _create(client, scenario="A", photos=1, plate="KA3002AA", phone="0670003002", works="Работа (цифры)").json()["url"]
    before = _badges(client, "/orders").get("green", 0)
    with SessionLocal() as db:
        wo = db.get(WorkOrder, _wo(url).id)
        wo.status = "ready"
        db.commit()
    assert _badges(client, "/orders").get("green", 0) == before + 1
    client.post("/logout")
    # Слесарь чужих цифр не видит
    _as(client, "andrey")
    assert "/purchases" not in client.get("/api/notify").json()["badges"]


def test_pairs_move_partsman(client):
    mgr2 = _second_manager()
    oleg = _uid("oleg")
    _as(client, "director")
    page = client.get("/settings/pairs").text
    assert "Менеджеры и запчастисты" in page and "Олег Запчастин" in page
    client.post("/settings/pairs", data={"partsman_id": str(oleg), "manager_id": str(mgr2)})
    with SessionLocal() as db:
        assert db.get(User, oleg).works_with_id == mgr2
    # Поменять местами запчастистов двух менеджеров
    irina = _uid("irina")
    client.post("/settings/pairs", data={"swap_a": str(irina), "swap_b": str(mgr2)})
    with SessionLocal() as db:
        assert db.get(User, oleg).works_with_id == irina
    client.post("/logout")
    _as(client, "irina")
    assert client.get("/settings/pairs").status_code == 403
