"""Утверждение цен разбора (18.11.2026, брат): новая деталь — «Не утверждено», продать нельзя; директор утверждает
(может поправить цену) или отказывает с причиной → «Отказано», можно отправлять ещё раз, висит в «Отказано», пока не
утвердят; после утверждения кладовщик цену не меняет — только запрос на изменение; цифры в меню у директора и кладовщика."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Donor, StockItem
from app.models.razbor import RazborPrice

from .conftest import login

FETCH = {"x-requested-with": "fetch"}


def _item(name):
    from app.models import PartCatalog
    with SessionLocal() as db:
        return db.scalar(select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id)
                         .where(PartCatalog.name == name))


def _req(item_id, kind="new"):
    with SessionLocal() as db:
        return db.scalar(select(RazborPrice).where(RazborPrice.stock_item_id == item_id, RazborPrice.kind == kind)
                         .order_by(RazborPrice.created_at.desc()))


def _badges(client, href):
    return {b["c"]: b["n"] for b in client.get("/api/notify", headers=FETCH).json()["badges"].get(href, [])}


def test_price_approval_flow(client):
    login(client, "dmitro", "dmitro123")      # кладовщик разбора
    client.post("/razbor/donors", data={"make": "Opel (цены)", "model": "Astra J", "year": "2013"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == "Opel (цены)"))
    for name, price in (("Фара (цены)", "3000"), ("Дверь (цены)", "4000")):
        client.post(f"/razbor/donors/{d.id}/parts", data={"name": name, "price": price, "qty": "1", "catsub": "Оптика||Фары"})
    fara, door = _item("Фара (цены)"), _item("Дверь (цены)")
    assert fara.price_state == "pending" and door.price_state == "pending"
    # не утверждена — продать / зарезервировать нельзя
    client.post(f"/razbor/parts/{fara.id}/sell", data={"action": "sold", "price": "3000", "delivery": "pickup", "pay_method": "cash"})
    with SessionLocal() as db:
        from app.services import stock as ssvc
        assert ssvc.balances(db, fara.branch_id, [fara.id])[fara.id]["on_hand"] == 1
    assert "Не утверждено" in client.get(f"/razbor/parts/{fara.id}").text
    client.post("/logout")

    # директор: цифра «новые», утверждает фару с поправкой цены, двери отказывает
    login(client, "director", "director123")
    assert _badges(client, "/razbor/prices").get("red", 0) >= 2
    page = client.get(f"/razbor/prices?tab=new&donor={d.id}").text
    assert "Фара (цены)" in page and "Дверь (цены)" in page
    client.post(f"/razbor/prices/{_req(fara.id).id}/approve", data={"price": "3500"})
    r = client.post(f"/razbor/prices/{_req(door.id).id}/decline", data={"answer": ""})   # без причины — нельзя
    assert _item("Дверь (цены)").price_state == "pending"
    client.post(f"/razbor/prices/{_req(door.id).id}/decline", data={"answer": "На фото трещина — пересними"})
    fara, door = _item("Фара (цены)"), _item("Дверь (цены)")
    assert fara.price_state == "" and fara.sale_price == 3500
    assert door.price_state == "declined"
    client.post("/logout")

    # кладовщик разбора: «Отказано» — цифра, причина; отправляет ещё раз — всё равно висит в «Отказано»
    login(client, "dmitro", "dmitro123")
    assert _badges(client, "/razbor").get("red", 0) >= 1
    page = client.get(f"/razbor/parts/{door.id}").text
    assert "Отказано в утверждении" in page and "трещина" in page
    client.post(f"/razbor/parts/{door.id}/resubmit", data={"note": "Переснял фото"})
    assert _item("Дверь (цены)").price_state == "declined" and _req(door.id).repeat
    assert "Дверь (цены)" in client.get("/razbor?f=declined").text
    # утверждённую цену сам не меняет — только запрос
    client.post(f"/razbor/parts/{fara.id}", data={"name": "Фара (цены)", "price": "1"})
    assert _item("Фара (цены)").sale_price == 3500
    client.post(f"/razbor/parts/{fara.id}/price-request", data={"price": "3200", "note": "На рынке дешевле"})
    assert _req(fara.id, "change").status == "pending"
    client.post("/logout")

    # директор: «Изменение цены» 🟠, повторная дверь — в «Новых» с пометкой; утверждает обе
    login(client, "director", "director123")
    assert _badges(client, "/razbor/prices").get("orange", 0) >= 1
    assert "На рынке дешевле" in client.get("/razbor/prices?tab=change").text
    assert "раньше отказано" in client.get(f"/razbor/prices?tab=new&donor={d.id}").text
    client.post(f"/razbor/prices/{_req(fara.id, 'change').id}/approve", data={"price": ""})
    client.post(f"/razbor/prices/{_req(door.id).id}/approve", data={"price": ""})
    assert _item("Фара (цены)").sale_price == 3200
    assert _item("Дверь (цены)").price_state == ""
    assert "утверждено" in client.get("/razbor/prices?tab=history").text
    client.post("/logout")

    # теперь продать можно
    login(client, "dmitro", "dmitro123")
    client.post(f"/razbor/parts/{door.id}/sell", data={"action": "sold", "price": "4000", "delivery": "pickup", "pay_method": "cash"})
    with SessionLocal() as db:
        from app.services import stock as ssvc
        assert ssvc.balances(db, door.branch_id, [door.id])[door.id]["on_hand"] == 0
    client.post("/logout")


def test_menu_and_rights(client):
    login(client, "irina", "irina123")   # менеджер не утверждает цены
    assert client.get("/razbor/prices", follow_redirects=False).status_code in (302, 303, 403)
    client.post("/logout")
    login(client, "deputy", "deputy123")
    assert client.get("/razbor/prices").status_code == 200
    client.post("/logout")
