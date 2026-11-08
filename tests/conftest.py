import os
import tempfile

import pytest

# Тесты работают на отдельной временной базе, рабочую data/crm.db не трогают
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db".replace("\\", "/")

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
