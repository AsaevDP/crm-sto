"""Клиенты (10.11.2026): вкладки, сортировка, фильтр по марке, «постоянный» вручную и с 3 визитов."""
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Client

from .test_step3 import _create
from .test_step6 import _as


def _client(phone):
    from app.services.orders import norm_phone
    with SessionLocal() as db:
        return db.scalar(select(Client).where(Client.phone == norm_phone(phone)))


def test_client_tabs_sort_regular(client):
    _as(client, "irina")
    for i in range(3):  # три визита одного клиента → постоянный сам
        _create(client, plate=f"AA30{i}0KL", phone="0673330000", make="Skoda", model="Fabia")
    _create(client, plate="AA4040KL", phone="0674440000", make="Nissan", model="Leaf")

    page = client.get("/clients").text
    # Плитки групп (11.11.2026): заголовок и ссылка-фильтр
    assert all(f'<span class="ptile-t">{t}</span>' in page for t in ("Постоянные", "Давно"))
    assert "?tab=regular" in page and "?tab=debt" not in page   # «Должники» убраны: в долг не работаем
    reg = client.get("/clients?tab=regular").text
    c3 = _client("0673330000")
    c1 = _client("0674440000")
    assert str(c3.id) in reg and str(c1.id) not in reg

    # Вручную в постоянные
    client.post(f"/clients/{c1.id}/regular")
    assert _client("0674440000").is_regular
    assert str(c1.id) in client.get("/clients?tab=regular").text
    card = client.get(f"/clients/{c1.id}").text
    assert "★ Постоянный" in card and "визитов: 1" in card

    # Фильтр по марке и сортировки
    nis = client.get("/clients?make=Nissan").text
    assert str(c1.id) in nis and str(c3.id) not in nis
    for s in ("name", "make", "plate", "visits", "spent", "last"):
        assert client.get(f"/clients?sort={s}").status_code == 200
    assert str(c3.id) in client.get("/clients?q=0673330000").text
