"""Очередь мойки у мойщика и детейлера (21.11.2026, брат): мойщику — только машины, где есть мойка; детейлеру — только
где есть его услуга; есть и мойка, и полировка — машину видят оба. Работа с ремонта без услуги прайса — по должности
исполнителя (записана на детейлера — мойщику не показываем)."""
from app.db import SessionLocal, current_branch_id

from .conftest import login
from .test_wash import _intake, _wo_by_plate
from .test_wash_queue import _user


def _queue_has(client, plate_words):
    page = client.get("/wash/queue").text
    q = page.split('id="queue"')[1].split("Моют другие")[0].split("Готовы")[0]
    return plate_words in q


def test_queue_only_own_kind(client):
    login(client, "irina", "irina123")
    client.get("/wash/prices")
    assert _intake(client, "АА 7711 РР", "0677770111", ["Полировка кузова"]).json()["ok"]
    assert _intake(client, "АА 7712 РР", "0677770112", ["Мойка кузова ручная", "Полировка кузова"]).json()["ok"]
    client.post("/logout")
    current_branch_id.set(_wo_by_plate("АА 7711 РР").branch_id)
    login(client, "moyka", "moyka123")
    assert not _queue_has(client, "7711") and _queue_has(client, "7712")
    client.post("/logout")
    login(client, "detail", "detail123")
    assert _queue_has(client, "7711") and _queue_has(client, "7712")
    client.post("/logout")
    # служебная проверка: функция по должности
    from app.services import wash as wsvc
    with SessionLocal() as db:
        wo = _wo_by_plate("АА 7711 РР")
        c = next(c for c in wsvc.board(db, wo.branch_id)["queue"] if c.o.id == wo.id)
        assert not wsvc.concerns(_user("moyka"), c) and wsvc.concerns(_user("detail"), c)
