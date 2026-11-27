"""27.11.2026 (брат): дела сотрудников — у директора, зама и админа тоже, с сиреневой обводкой («для сведения»);
то, что решают они сами, — без обводки. «Открыть заново» — кружок у «Клиентов». «Всегда в наличии» — плитки-фильтры."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import ChemRequest, PartCatalog, User
from app.services import badges as bsvc

from .conftest import login

FETCH = {"X-Requested-With": "fetch"}


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _badges(c):
    bsvc._CACHE.clear()
    return c.get("/api/notify", headers=FETCH).json()["badges"]


def test_worker_task_shown_to_bosses_as_info(client):
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login == "admin"))
        current_branch_id.set(u.branch_id)
        c = PartCatalog(name="Тест-химия для руководства", section="chem")
        db.add(c)
        db.flush()
        db.add(ChemRequest(catalog_id=c.id, qty=1))
        db.commit()
        c_id = c.id
    for who in ("director", "deputy", "admin"):
        _as(client, who)
        items = [x for lst in _badges(client).values() for x in lst
                 for g in x["go"] if "просят химию для мойки" in g["t"]]
        assert items, who
        assert all(x["info"] for x in items), who   # дело кладовщика — «для сведения»
    _as(client, "pavel")   # самому кладовщику — обычный кружок
    own = [x for x in _badges(client).get("/stock/issues", []) if "просят химию" in x["t"]]
    assert own and not own[0]["info"]
    from sqlalchemy import delete
    with SessionLocal() as db:
        db.execute(delete(ChemRequest).where(ChemRequest.catalog_id == c_id))
        db.commit()


def test_hide_info_badges(client):
    """Директор выключил «для сведения» — сиреневых кружков нет, свои остались; админ выключил — сам не включит."""
    from app.models import OrderReopen, UserPermission, WorkOrder
    with SessionLocal() as db:
        d = db.scalar(select(User).where(User.login == "director"))
        current_branch_id.set(d.branch_id)
        c = PartCatalog(name="Тест-химия скрыть", section="chem")
        db.add(c)
        db.flush()
        db.add(ChemRequest(catalog_id=c.id, qty=1))
        wo = db.scalar(select(WorkOrder.id).limit(1))
        if wo:
            db.add(OrderReopen(work_order_id=wo, user_id=d.id, reason="тест"))
        db.commit()
        did, c_id = d.id, c.id

    def all_items():
        return [x for lst in _badges(client).values() for x in lst]
    _as(client, "director")
    assert any(x["info"] for x in all_items())
    assert "Кружки «для сведения»" in client.get("/settings").text
    client.post("/settings/my/info-badges", data={})            # выключил сам
    items = all_items()
    assert not any(x["info"] for x in items)
    if wo:
        assert any("открыть наряд заново" in x["t"] for x in items)   # своё дело осталось
    client.post("/settings/my/info-badges", data={"show": "1"})  # включил обратно
    assert any(x["info"] for x in all_items())
    with SessionLocal() as db:   # админ выключил директору правом
        current_branch_id.set(db.get(User, did).branch_id)
        db.add(UserPermission(user_id=did, permission="badges.hide_info", allow=True))
        db.commit()
    _as(client, "director")
    assert not any(x["info"] for x in all_items())
    assert "выключено админом" in client.get("/settings/my").text
    assert client.post("/settings/my/info-badges", data={"show": "1"}).status_code in (403, 303, 200)
    assert not any(x["info"] for x in all_items())
    # админ: право у него есть всегда, но для него ничего не значит — видит «для сведения»
    _as(client, "admin")
    assert any(x["info"] for x in all_items())
    # убрать за собой — база общая для всех тестов
    from sqlalchemy import delete
    with SessionLocal() as db:
        current_branch_id.set(db.get(User, did).branch_id)
        db.execute(delete(UserPermission).where(UserPermission.user_id == did, UserPermission.permission == "badges.hide_info"))
        db.execute(delete(OrderReopen).where(OrderReopen.reason == "тест"))
        db.execute(delete(ChemRequest).where(ChemRequest.catalog_id == c_id))
        db.commit()


def test_keep_tiles_filter(client):
    _as(client, "director")
    page = client.get("/stock/always").text
    assert 'ptile-t">Все' in page and 'ptile-t">Не хватает' in page and "Почти на норме" not in page
    assert client.get("/stock/always?f=red").status_code == 200
