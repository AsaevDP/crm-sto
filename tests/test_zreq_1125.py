"""25.11.2026 (брат): правки по проверке под менеджером и запчастистом.
- «Табель»: «Часов за месяц» и чужие часы — только директору, заму, админу, бухгалтеру; менеджер «Напомнить» тем, кто
  не отметил уход (🔴 у «Табеля», гаснет после напоминания; время он не вписывает).
- «Инструмент → Просят / сдают» — своё окно раздела, не «Запросы на выдачу».
- «Заказы запчастистов»: цифры людей — по открытой вкладке.
- Запчастисту после «Согласовал с клиентом» — 🟠 у «Нарядов» «закажите у поставщика».
- «Всегда в наличии»: склад — кладовщик, химия — запчастист; уже заказанное второй раз не заказывается."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Notification, PartCatalog, Purchase, StockKeep, User
from app.services import lateness as ls
from app.services import settings as st

from .conftest import agree, login
from .test_lateness import _ev, _user
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _badges(c):
    return c.get("/api/notify", headers=FETCH).json()["badges"]


def _has(b, menu, color, text):
    return any(x["c"] == color and any(text in g["t"] for g in (x.get("go") or [x])) for x in b.get(menu, []))


def test_timesheet_manager_own_hours_and_remind(client):
    today = date.today()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    with SessionLocal() as db:
        u = _user(db, "noout1125", "mechanic", d - timedelta(days=3))
        bid = u.branch_id
        current_branch_id.set(bid)
        old = st.get(db, bid, "attendance").get("alerts_from")
        st.put(db, bid, "attendance", {"alerts_from": (d - timedelta(days=1)).isoformat()})
        _ev(db, u, d, 8, 0, "in")                       # пришёл, ухода нет
        db.commit()
        uid = u.id
    try:
        login(client, "irina", "irina123")
        # Часы: только свои
        page = client.get("/timesheet?v=hours").text
        assert "Тест noout1125" not in page and "ваши" in client.get("/timesheet").text
        # Чужой табель менеджеру не открывается — показывается свой
        assert "Мой табель" in client.get(f"/timesheet?user={uid}").text
        # «Забыли отметить уход»: 🔴 у «Табеля», строка с «Напомнить», без «Вписать время»
        assert _has(_badges(client), "/timesheet", "red", "напомните")
        page = client.get("/timesheet?v=open").text
        assert "Тест noout1125" in page and ">Напомнить<" in page and "Вписать время" not in page
        client.post("/timesheet/remind", data={"user_id": str(uid), "day": d.isoformat()})
        with SessionLocal() as db:
            n = db.scalars(select(Notification).where(Notification.user_id == uid, Notification.kind == "att_remind")).all()
        assert len(n) == 1 and "сообщите бухгалтеру" in n[0].text.lower()
        assert "✓ напомнили" in client.get("/timesheet?v=open").text
        page = client.get("/timesheet?v=open").text   # строка этого сотрудника больше не мигает (напомнили)
        row = page[page.index("Тест noout1125") - 400: page.index("Тест noout1125")]
        assert 'data-open="1"' not in row.rsplit("<tr", 1)[-1]
        client.post("/logout")
        # Бухгалтер — все часы и «Вписать время»
        login(client, "buh", "buh123")
        assert "Тест noout1125" in client.get("/timesheet?v=hours").text
        assert "Вписать время" in client.get("/timesheet?v=open").text
        client.post("/logout")
    finally:
        with SessionLocal() as db:
            current_branch_id.set(bid)
            st.put(db, bid, "attendance", {"alerts_from": old or date.today().isoformat()})
            db.get(User, uid).active = False
            db.commit()


def test_tools_reqs_own_view_and_partsman_tiles(client):
    login(client, "irina", "irina123")
    page = client.get("/tools").text
    assert 'href="/tools?view=reqs"' in page and 'href="/stock/issues?tab=tools"' not in page
    assert "Просят и сдают инструмент" in client.get("/tools?view=reqs").text
    # «Заказы запчастистов»: подписи людей — по открытой вкладке
    page = client.get("/purchases/by-partsman?st=arrived").text
    assert "все люди · приняты" in page
    client.post("/logout")


def test_partsman_order_badge_after_approval(client):
    login(client, "irina", "irina123")
    url = _create(client, scenario="A", photos=1, plate="КА 2525 КА", phone="0502525001").json()["url"]
    client.post("/logout")
    login(client, "oleg", "oleg123")
    client.post(f"{url}/parts", data={"name": "Помпа 1125", "qty": "1", "cost": "900", "markup_pct": "30", "action": "order"})
    client.post("/logout")
    agree(client, url, then="oleg")
    assert _has(_badges(client), "/orders", "orange", "закажите у поставщика")
    page = client.get(url).text
    assert 'id="btn-order-wo"' in page and 'data-draft="1"' in page


def test_keep_owner_and_no_double_order(client):
    with SessionLocal() as db:
        pv = db.scalar(select(User).where(User.login == "pavel"))
        current_branch_id.set(pv.branch_id)
        c = PartCatalog(name="Шампунь 1125", section="chem", category="Шампуни", unit="шт")
        db.add(c)
        db.flush()
        db.add(StockKeep(catalog_id=c.id, min_qty=Decimal(3)))
        db.commit()
        cid = c.id
    # Кладовщик химию не заказывает — это запчастист
    login(client, "pavel", "pavel123")
    assert client.post("/stock/always/order/missing", data={"cid": str(cid)}).status_code == 403
    assert "Заказывает запчастист" in client.get("/stock/always").text
    client.post("/logout")
    login(client, "oleg", "oleg123")
    page = client.get("/wash?tab=chem").text
    assert "Шампунь 1125" in page and "Заказать 3" in page
    r = client.post("/stock/always/order/missing", data={"cid": str(cid), "back": "/wash?tab=chem"})
    assert "/purchases/" in str(r.url)
    with SessionLocal() as db:
        ps = db.scalars(select(Purchase).where(Purchase.note.like("%Всегда в наличии%"), Purchase.kind == "chem")).all()
    assert len(ps) == 1
    # Второй раз — не заказывается (уже в черновике), видно кто заказал
    client.post("/stock/always/order/missing", data={"cid": str(cid), "back": "/wash?tab=chem"})
    with SessionLocal() as db:
        assert len(db.scalars(select(Purchase).where(Purchase.note.like("%Всегда в наличии%"), Purchase.kind == "chem")).all()) == 1
    page = client.get("/wash?tab=chem").text
    assert "в черновике 3" in page and "Заказать 3" not in page
