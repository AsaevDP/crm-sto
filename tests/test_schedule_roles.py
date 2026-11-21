"""«Запись» и «Загрузка мастеров» (22.11.2026, брат): «Новую запись» видят только те, кто записывает (менеджер,
директор, зам, админ) — у мастеров её нет; директор записывает клиентов, как менеджер; «Загрузка мастеров» — кнопкой
с «Пульта» (менеджер, директор, зам, админ), мастерам страница закрыта."""
from .conftest import login


def test_masters_have_no_new_appointment_form(client):
    for lg in ("andrey", "moyka"):
        login(client, lg, lg + "123")
        for v in ("", "?p=week"):
            page = client.get("/schedule" + v).text
            assert 'id="appt-form"' not in page and "Новая запись" not in page
        client.post("/logout")


def test_director_can_book(client):
    login(client, "director", "director123")
    page = client.get("/schedule").text
    assert 'id="appt-form"' in page and ">Записать<" in page
    r = client.post("/schedule", data={"day": "2030-01-10", "time": "10:00", "duration": "60", "client_name": "Директор записал",
                                       "phone": "0671112233", "car": "Kia Rio"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Директор записал" in client.get("/schedule?d=2030-01-10").text
    assert client.get("/schedule/remind").status_code == 200
    client.post("/logout")


def test_load_page_on_pult(client):
    for lg in ("irina", "director", "deputy", "admin"):
        login(client, lg, lg + "123")
        assert 'href="/load"' in client.get("/pult").text
        for p in ("day", "week", "month"):
            page = client.get(f"/load?p={p}").text
            assert "Загрузка мастеров" in page and "загрузка СТО" in page and "ld-row" in page
        client.post("/logout")
    login(client, "andrey", "andrey123")
    assert client.get("/load", follow_redirects=False).status_code in (303, 403)
    client.post("/logout")
