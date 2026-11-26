"""26.11.2026 (брат): карточка детали разбора — всё для сайта в одном блоке: «📷 Добавить фото» (сразу загружается),
«✕» удалить фото, «Сохранить описание», «✓ Выложил на сайт». Форма «Изменить» описание не стирает."""
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Donor, StockItem, StockPhoto

from .conftest import approve_razbor, login

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


def _photos(iid):
    with SessionLocal() as db:
        return db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id == iid).order_by(StockPhoto.created_at)).all()


def test_site_block_photos_description_post(client):
    login(client, "dmitro", "dmitro123")
    client.post("/razbor/donors", data={"make": "Opel-блок", "model": "Astra", "year": "2014"})
    with SessionLocal() as db:
        d = db.scalar(select(Donor).where(Donor.make == "Opel-блок"))
    client.post(f"/razbor/donors/{d.id}/parts/bulk", data={"category": "", "subcategory": "", "lines": "Капот блок-тест; 3800"})
    with SessionLocal() as db:
        iid = db.scalar(select(StockItem.id).where(StockItem.donor_id == d.id))
    # пока цена ждёт директора — блок уже есть, фото и описание можно готовить, «Выложил» недоступна
    page = client.get(f"/razbor/parts/{iid}").text
    assert 'id="site"' in page and "Добавить фото" in page and "Ждёт цены директора" in page
    client.post(f"/razbor/parts/{iid}/photos", files=[("photos", ("a.jpg", JPEG, "image/jpeg")), ("photos", ("b.jpg", JPEG, "image/jpeg"))])
    client.post(f"/razbor/parts/{iid}/description", data={"description": "Без вмятин"})
    assert len(_photos(iid)) == 2
    approve_razbor()
    # «Изменить» (без поля описания) описание не стирает
    client.post(f"/razbor/parts/{iid}", data={"name": "Капот блок-тест", "condition": "good"})
    with SessionLocal() as db:
        assert db.get(StockItem, iid).description == "Без вмятин"
    client.post(f"/razbor/parts/{iid}/to-site")
    with SessionLocal() as db:
        assert db.get(StockItem, iid).publish_on_site
    # удалили фото — меньше 2 — снова «Выложить»
    client.post(f"/razbor/parts/{iid}/photos/{_photos(iid)[0].id}/delete")
    assert len(_photos(iid)) == 1
    with SessionLocal() as db:
        assert not db.get(StockItem, iid).publish_on_site
    # список «Разбора»: заметная кнопка и что не хватает
    lst = client.get("/razbor?v=parts&f=nosite").text
    assert "📷 Выложить на сайт" in lst and "не хватает: фото: 1 из 2" in lst
    # кружки у «Разбора»: 🔴 выложить на сайт (+ 🟠 не утверждено / в резерве, когда есть)
    b = client.get("/api/notify", headers={"X-Requested-With": "fetch"}).json()["badges"].get("/razbor", [])
    items = [g for x in b for g in x.get("go", [x])]
    assert any("выложить на сайт" in g["t"] for g in items)
    # «На сайт» — крупный переключатель
    assert 'class="sn-switch"' in client.get("/site").text
