"""25.11.2026 (брат, наряд 39): клиент согласует и ЗАПЧАСТИ — как работы. На шаге «Звонок клиенту» у каждой запчасти
«✓ Да, берём / ✕ Клиент отказался», у группы вариантов — «Выбрать» или «Клиент отказался от всех». «Согласовал с
клиентом» появляется в самом конце, когда отмечено всё; без запчастей — сразу после работ. Отказ → архив наряда (в счёт и
бумаги не идёт) + карта клиента «Отказался от запчастей»; невыбранный вариант — не отказ."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ClientEvent, WorkOrder, WoPart, WoWork

from .conftest import login
from .test_step3 import _create

FETCH = {"x-requested-with": "fetch"}


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def _parts(wid):
    with SessionLocal() as db:
        return {p.name: p for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wid))}


def _to_call(client, plate, phone, parts=True):
    """Наряд до шага «Звонок»: проблема, работа, подбор (датчик под заказ + 2 варианта диска)."""
    login(client, "irina", "irina123")
    url = _create(client, scenario="B", photos=1, plate=plate, phone=phone).json()["url"]
    client.post("/logout")
    login(client, "andrey", "andrey123")
    client.post(f"{url}/findings", data={"text": "Диски и датчик", "urgency": "soon"}, headers=FETCH)
    client.post(f"{url}/diag-done")
    client.post("/logout")
    login(client, "irina", "irina123")
    client.post(f"{url}/works", data={"name": "Замена дисков", "pricing": "fixed", "rate": "900", "qty": "1"})
    client.post(f"{url}/works-sent", data={"no_parts": "1"} if not parts else {})
    if parts:
        client.post("/logout")
        login(client, "oleg", "oleg123")
        client.post(f"{url}/parts", data={"name": "Датчик износа колодок", "qty": "1", "cost": "280", "markup_pct": "30",
                                          "action": "order"})
        client.post(f"{url}/parts", data={"name": "Комплект направляющих", "qty": "1", "cost": "420", "markup_pct": "30",
                                          "action": "order"})
        client.post(f"{url}/parts", data={"name": "Диск тормозной передний", "brand": "Bosch", "qty": "2", "cost": "900",
                                          "markup_pct": "30", "action": "option", "option_group": ""})
        g = _parts(_wo(url).id)["Диск тормозной передний"].option_group
        client.post(f"{url}/parts", data={"name": "Диск тормозной передний", "brand": "Brembo", "qty": "2", "cost": "1200",
                                          "markup_pct": "30", "action": "option", "option_group": g})
        client.post(f"{url}/picking-done")
        client.post("/logout")
        login(client, "irina", "irina123")
    with SessionLocal() as db:
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == _wo(url).id))
    client.post(f"{url}/works/{w.id}/approval", data={"value": "yes"}, headers=FETCH)
    return url


def test_parts_need_answer_then_button(client):
    url = _to_call(client, "КА 2511 КА", "0502511001")
    wid = _wo(url).id
    ps = _parts(wid)
    assert all(p.approval == "pending" for p in ps.values())                 # до согласования — ждут ответа клиента
    page = client.get(url).text
    assert "✓ Да, берём" in page and "Клиент отказался от всех вариантов" in page
    assert 'id="btn-approve-done"' not in page and "К запчастям ↓" in page   # работы отмечены — дальше запчасти
    assert "Отметьте по каждой запчасти" in client.post(f"{url}/approval-done").text or \
           "ещё не выбрал вариант" in client.post(f"{url}/approval-done").text

    # датчик — да, направляющие — нет; вариант не выбран — кнопки нет
    client.post(f"{url}/parts/{ps['Датчик износа колодок'].id}/approval", data={"value": "yes"}, headers=FETCH)
    client.post(f"{url}/parts/{ps['Комплект направляющих'].id}/approval", data={"value": "no"}, headers=FETCH)
    page = client.get(url).text
    assert 'id="btn-approve-done"' not in page and "Выбрать вариант" in page
    assert "ещё не выбрал вариант" in client.post(f"{url}/approval-done").text
    # выбрали Brembo — всё отмечено, кнопка появилась
    brembo = next(p for p in _parts(wid).values() if p.option_group and p.price > 3000 // 2 and p.status == "option"
                  and p.cost > 1000)
    client.post(f"{url}/parts/{brembo.id}/choose")
    assert 'id="btn-approve-done"' in client.get(url).text
    client.post(f"{url}/approval-done")
    assert _wo(url).status in ("picking", "waiting", "instock")

    ps = _parts(wid)
    assert ps["Комплект направляющих"].status == "archived"                  # отказ — в архив наряда
    assert ps["Датчик износа колодок"].status not in ("archived",)
    with SessionLocal() as db:
        ev = db.scalars(select(ClientEvent).where(ClientEvent.work_order_id == wid, ClientEvent.kind == "part_refused")).all()
        cid = _wo(url).client_id
    assert len(ev) == 1 and "Комплект направляющих" in ev[0].text            # невыбранный Bosch — не отказ
    card = client.get(f"/clients/{cid}").text
    assert "Отказался от запчастей · 1" in card
    r = client.get(f"{url}/print")
    assert r.status_code == 200 and "Датчик износа колодок" in r.text
    assert "Комплект направляющих" not in r.text                             # в бумаги клиенту не идёт


def test_group_refuse_all_and_no_parts(client):
    url = _to_call(client, "КА 2512 КА", "0502511002")
    wid = _wo(url).id
    ps = _parts(wid)
    for n in ("Датчик износа колодок", "Комплект направляющих"):
        client.post(f"{url}/parts/{ps[n].id}/approval", data={"value": "yes"}, headers=FETCH)
    disk = ps["Диск тормозной передний"]
    client.post(f"{url}/parts/{disk.id}/approval", data={"value": "no"})     # от всех вариантов
    page = client.get(url).text
    assert "клиент отказался от всех" in page and 'id="btn-approve-done"' in page
    client.post(f"{url}/approval-done")
    assert all(p.status == "archived" for p in _parts(wid).values() if p.option_group)
    with SessionLocal() as db:
        ev = db.scalars(select(ClientEvent).where(ClientEvent.work_order_id == wid, ClientEvent.kind == "part_refused")).all()
    assert len(ev) == 1 and "все варианты" in ev[0].text

    # без запчастей — кнопка сразу после работ
    client.post("/logout")
    url2 = _to_call(client, "КА 2513 КА", "0502511003", parts=False)
    assert 'id="btn-approve-done"' in client.get(url2).text
