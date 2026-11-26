"""26.11.2026 (брат): шаг «✓ Выложил на сайт».
- директор утвердил цену (б/у в «Ценах разбора», новую в «На сайт») → кладовщику разбора сообщение «выложите на сайт»
  и 🔴 у «На сайт»; деталь в «Выложить на сайт», в выгрузку не идёт;
- «Выложил» — только с 2 фото и описанием; потом «Выложены на сайт» и выгрузка; сообщение «✓ сделано»;
- новая деталь: отказ директора → «Отправить ещё раз» с комментарием → снова «Ждут утверждения»;
  директор при утверждении может поправить цену."""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, StockItem, User
from app.models.razbor import RazborPrice, SiteNew, SiteNewPhoto

from .conftest import login
from .test_site import _leftover_for_sale

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PHOTOS = [("photos", ("a.jpg", JPEG, "image/jpeg")), ("photos", ("b.jpg", JPEG, "image/jpeg"))]


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _site_badges(c):
    return c.get("/api/notify").json()["badges"].get("/site", [])


def _post_notes(uid):
    with SessionLocal() as db:
        return db.scalars(select(Notification).where(Notification.user_id == uid, Notification.kind == "site_post")).all()


def test_used_part_approved_notify_post(client):
    _as(client, "dmitro")
    client.post("/razbor/donors", data={"make": "Skoda-пост", "model": "Octavia", "year": "2015"})
    with SessionLocal() as db:
        from app.models import Donor
        d = db.scalar(select(Donor).where(Donor.make == "Skoda-пост"))
        dm = db.scalar(select(User).where(User.login == "dmitro")).id
    client.post(f"/razbor/donors/{d.id}/parts/bulk", data={"category": "Оптика", "subcategory": "Фары",
                                                           "lines": "Фара пост-тест; 2500"})
    with SessionLocal() as db:
        i = db.scalar(select(StockItem).where(StockItem.donor_id == d.id))
        r = db.scalar(select(RazborPrice).where(RazborPrice.stock_item_id == i.id, RazborPrice.status == "pending"))
        iid, rid = i.id, r.id
        assert not i.publish_on_site
    before = len(_post_notes(dm))
    _as(client, "director")
    client.post(f"/razbor/prices/{rid}/approve", data={"price": "2600"})
    assert len(_post_notes(dm)) == before + 1   # кладовщику разбора — «цену утвердили, выложите на сайт»
    _as(client, "dmitro")
    assert any(x["c"] == "red" and "б/у" in x["t"] for x in _site_badges(client))
    page = client.get("/site?t=used&f=post").text
    assert "Фара пост-тест" in page and "Не хватает" in page and "Выложить на сайт" in page
    card = client.get(f"/razbor/parts/{iid}").text
    assert 'id="site"' in card and "Фото · 0 из 2" in card and "disabled" in card
    # в выгрузке нет, пока не выложили
    assert "Фара пост-тест" not in client.get("/razbor/export.json").text
    client.post(f"/razbor/parts/{iid}", data={"name": "Фара пост-тест", "description": "Без трещин"}, files=PHOTOS)
    client.post(f"/razbor/parts/{iid}/to-site")
    with SessionLocal() as db:
        assert db.get(StockItem, iid).publish_on_site
    assert "Фара пост-тест" in client.get("/razbor/export.json").text
    assert "Фара пост-тест" in client.get("/site?t=used&f=site").text
    # дело сделано — сообщение не горит (если больше нечего выкладывать)
    from app.services import notes, site
    with SessionLocal() as db:
        u = db.get(User, dm)
        n = db.scalars(select(Notification).where(Notification.user_id == dm, Notification.kind == "site_post")
                       .order_by(Notification.created_at.desc())).first()
        assert notes.is_done(db, n, u) == (site.to_post_total(db, u.branch_id) == 0)


def test_new_part_decline_resubmit_price_and_post(client):
    cat, lo_id, wid = _leftover_for_sale(client, "Стойка пост-тест")
    _as(client, "oleg")
    client.post(f"/stock/leftovers/{lo_id}", data={"action": "sale"})
    with SessionLocal() as db:
        sid = db.scalar(select(SiteNew.id).where(SiteNew.leftover_id == lo_id))
    # 26.11.2026: пока ждёт директора — у кладовщика разбора 🟠 у «На сайт» (подготовить фото и описание)
    _as(client, "dmitro")
    assert any(x["c"] == "orange" and "ждут утверждения" in x["t"] for x in _site_badges(client))
    _as(client, "director")
    assert not any(x["c"] == "orange" and "ждут утверждения" in x["t"] for x in _site_badges(client))
    client.post(f"/site/new/{sid}/decline", data={"answer": "Дорого"})
    _as(client, "dmitro")
    assert any("отказал" in x["t"] for x in _site_badges(client))
    assert "Отправить ещё раз" in client.get("/site?t=new&f=declined").text
    client.post(f"/site/new/{sid}/resubmit", data={"note": ""})   # без комментария — нельзя
    with SessionLocal() as db:
        assert db.get(SiteNew, sid).state == "declined"
    client.post(f"/site/new/{sid}/resubmit", data={"note": "Проверил — на рынке так же"})
    with SessionLocal() as db:
        s = db.get(SiteNew, sid)
        assert s.state == "pending" and s.note
    _as(client, "director")
    assert "Проверил — на рынке так же" in client.get("/site?t=new&f=wait").text
    client.post(f"/site/new/{sid}/approve", data={"price": "600"})
    with SessionLocal() as db:
        s = db.get(SiteNew, sid)
        assert s.state == "approved" and s.price == Decimal("600") and s.posted_at is None
    _as(client, "dmitro")
    assert any(x["c"] == "red" and "новые" in x["t"] for x in _site_badges(client))
    client.post(f"/site/new/{sid}", data={"description": "Новая, в коробке"}, files=PHOTOS)
    client.post(f"/site/new/{sid}/post")
    with SessionLocal() as db:
        s = db.get(SiteNew, sid)
        assert s.posted_at is not None
        photo = db.scalars(select(SiteNewPhoto).where(SiteNewPhoto.site_new_id == sid)).first()
    # удалили фото — меньше 2 — снова «Выложить на сайт»
    client.post(f"/site/new/{sid}", data={"action": "photo_del", "photo": str(photo.id)})
    with SessionLocal() as db:
        assert db.get(SiteNew, sid).posted_at is None


def test_reset_posted_once():
    from app.models import Branch
    from app.services import settings as st
    from app.services import site
    from app.db import current_branch_id
    with SessionLocal() as db:
        br = db.scalar(select(Branch))
        current_branch_id.set(br.id)
        i = db.scalar(select(StockItem).where(StockItem.code.like("Р-%")))
        assert i is not None
        i.publish_on_site = True
        st.put(db, br.id, "grants_once", {"site.posted_reset": False})
        db.commit()
        site.reset_posted_once(db)
        db.commit()
        assert not db.get(StockItem, i.id).publish_on_site
        assert st.get(db, br.id, "grants_once").get("site.posted_reset")
