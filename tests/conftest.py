import os
import tempfile

import pytest

# Тесты работают на отдельной временной базе, рабочую data/crm.db не трогают
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db".replace("\\", "/")
os.environ["CRM_DATA_DIR"] = _tmp  # фото тестов — тоже во временную папку

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _test_cell_id():
    """19.11.2026 (брат): без ячейки на склад не принимают. Старые тесты принимают «как попало» — им кладовщик
    выбирает общую тестовую ячейку «ТЕСТ» (новые тесты на ячейку передают её сами)."""
    from sqlalchemy import select
    from app.db import SessionLocal, current_branch_id
    from app.models import Branch, Location
    with SessionLocal() as db:
        loc = db.scalar(select(Location).where(Location.rack == "ТЕСТ"))
        if not loc:
            bid = db.scalar(select(Branch.id))
            current_branch_id.set(bid)
            loc = Location(rack="ТЕСТ", shelf="1", place="1", label="ТЕСТ / 1 / 1", branch_id=bid)
            db.add(loc)
            db.commit()
        return str(loc.id)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        post = c.post

        def post_with_cell(url, *a, **kw):
            data = kw.get("data")
            if str(url).endswith("/receive") and isinstance(data, dict) and not any(
                    k.startswith("loc_") or k == "location_id" for k in data) and not data.get("no_cell"):
                kw["data"] = {**data, "location_id": _test_cell_id()}
            elif str(url).endswith("/receive") and isinstance(data, dict) and data.get("no_cell"):
                kw["data"] = {k: v for k, v in data.items() if k != "no_cell"}
            return post(url, *a, **kw)
        c.post = post_with_cell
        yield c


def login(c, login, password):
    r = c.post("/login", data={"login": login, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def approve_razbor():
    """18.11.2026 (брат): новые детали разбора ждут утверждения цены директором — в тестах продаж утверждаем все сразу."""
    from app.db import SessionLocal
    from app.models import StockItem
    from app.models.razbor import RazborPrice
    from sqlalchemy import select
    from app.db import current_branch_id
    from app.models import Branch
    with SessionLocal() as db:
        current_branch_id.set(db.scalar(select(Branch.id)))   # журнал пишется с филиалом
        for r in db.scalars(select(RazborPrice).where(RazborPrice.status == "pending", RazborPrice.kind == "new")):
            r.status, r.final_price = "approved", r.price
        for i in db.scalars(select(StockItem).where(StockItem.price_state.in_(("pending", "declined")))):
            i.price_state = ""
        db.commit()


def send_works(c, url, as_user="irina"):
    """23.11.2026 (брат): шаг «Работы» — менеджер жмёт «Работы записаны → на подбор» (если ещё не нажал)."""
    c.post("/logout")
    login(c, as_user, as_user + "123")
    r = c.post(f"{url}/works-sent")
    c.post("/logout")
    return r


def yes_all_works(url):
    """Звонок клиенту: все работы текущего круга — «✓ Да» (для тестов, где решение клиента не важно)."""
    import uuid as _u
    from app.db import SessionLocal, current_branch_id
    from app.models import WorkOrder, WoWork
    from sqlalchemy import select
    with SessionLocal() as db:
        wo = db.get(WorkOrder, _u.UUID(url.rsplit("/", 1)[1].split("#")[0]))
        current_branch_id.set(wo.branch_id)
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.approval == "pending")):
            w.approval = "yes"
        db.commit()


def agree(c, url, then="oleg", partsman="oleg"):
    """13.11.2026 (брат): запчастист подбирает на согласовании, заказ уходит поставщику только после того, как
    менеджер «Согласовал с клиентом». 23.11.2026: по шагам — менеджер «Работы записаны» (если ещё нет), запчастист
    «Подбор завершён», менеджер — «Да» по работам и «Согласовал»; дальше входим как then."""
    send_works(c, url)
    login(c, partsman, partsman + "123")
    c.post(f"{url}/picking-done")
    c.post("/logout")
    yes_all_works(url)
    login(c, "irina", "irina123")
    r = c.post(f"{url}/approval-done")
    c.post("/logout")
    if then:
        login(c, then, then + "123")
    return r
