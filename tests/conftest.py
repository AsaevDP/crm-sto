import os
import tempfile

import pytest

# Тесты работают на отдельной временной базе, рабочую data/crm.db не трогают
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db".replace("\\", "/")
os.environ["CRM_DATA_DIR"] = _tmp  # фото тестов — тоже во временную папку

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def login(c, login, password):
    r = c.post("/login", data={"login": login, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def agree(c, url, then="oleg", partsman="oleg"):
    """13.11.2026 (брат): запчастист подбирает на согласовании, заказ уходит поставщику только после того, как
    менеджер «Согласовал с клиентом». Запчастист — «Подбор завершён», менеджер — согласовал; дальше входим как then."""
    c.post("/logout")
    login(c, partsman, partsman + "123")
    c.post(f"{url}/picking-done")
    c.post("/logout")
    login(c, "irina", "irina123")
    r = c.post(f"{url}/approval-done")
    c.post("/logout")
    if then:
        login(c, then, then + "123")
    return r
