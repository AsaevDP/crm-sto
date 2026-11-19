"""20.11.2026 (брат): химию для мойки заказывает запчастист.

- Запчастист: в меню «Мойка» — только «Химия», «Просят», «Заканчивается», «Закупка для мойки», кнопки «Заказать химию» и
  «Химия на складе». Без очереди, приёмки, прайса (и по прямому адресу — нельзя).
- Менеджер: очередь, «Изменить очередь», химия, «Просят», «Принять на мойку», «Прайс» — но без заказа и «Моих закупок».
- Директор, зам, админ — всё как было (и следить, и заказать).
"""
from .conftest import login


def _tiles(html):
    return html.split('wash-tiles', 1)[1].split("</nav>", 1)[0] if "wash-tiles" in html else ""


def _nav(html):
    return html.split("<main", 1)[0]


def test_partsman_wash_chem_only(client):
    login(client, "oleg", "oleg123")
    assert 'href="/wash"' in _nav(client.get("/orders").text)
    page = client.get("/wash").text
    t = _tiles(page)
    assert "Химия" in t and "Просят" in t and "Заканчивается" in t and "Закупка для мойки" in t
    assert "Очередь" not in t and "Изменить очередь" not in t
    assert "Заказать химию" in page and "Химия на складе" in page
    assert "Принять на мойку" not in page and 'href="/wash/prices"' not in page
    assert "Изменить очередь" not in _tiles(client.get("/wash?tab=now").text)   # очередь — нельзя, откроется химия
    assert client.get("/wash/queue", follow_redirects=False).status_code == 403
    assert client.get("/wash/prices", follow_redirects=False).status_code == 403
    assert client.post("/wash/purchases/new", data={"supplier_new": "Химия (запчастист)"}).status_code == 200
    client.post("/logout")


def test_manager_wash_without_ordering(client):
    login(client, "irina", "irina123")
    page = client.get("/wash").text
    t = _tiles(page)
    assert "Очередь" in t and "Изменить очередь" in t and "Химия" in t and "Просят" in t
    assert "Заканчивается" not in t and "Закупка для мойки" not in t
    assert "Заказать химию" not in page and "Принять на мойку" in page and 'href="/wash/prices"' in page
    assert "Химия на складе" in page
    assert "Мои закупки" not in _nav(page)
    assert client.get("/wash?tab=buy").status_code == 200 and "Закупки химии" not in _tiles(client.get("/wash?tab=buy").text)
    client.post("/logout")


def test_director_wash_unchanged(client):
    login(client, "director", "director123")
    page = client.get("/wash").text
    t = _tiles(page)
    assert "Очередь" in t and "Закупка для мойки" in t and "Заказать химию" in page
    client.post("/logout")
