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
    for who in ("director", "deputy", "admin"):
        _as(client, who)
        items = [x for lst in _badges(client).values() for x in lst
                 for g in x["go"] if "просят химию для мойки" in g["t"]]
        assert items, who
        assert all(x["info"] for x in items), who   # дело кладовщика — «для сведения»
    _as(client, "pavel")   # самому кладовщику — обычный кружок
    own = [x for x in _badges(client).get("/stock/issues", []) if "просят химию" in x["t"]]
    assert own and not own[0]["info"]


def test_keep_tiles_filter(client):
    _as(client, "director")
    page = client.get("/stock/always").text
    assert 'ptile-t">Все' in page and 'ptile-t">Не хватает' in page and "Почти на норме" not in page
    assert client.get("/stock/always?f=red").status_code == 200
