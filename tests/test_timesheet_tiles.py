"""«Табель» (25.11.2026, брат): плитки двумя строками — 1) Сейчас на месте, Ушли, Не отмечались, Для сведения;
2) Нужно решить, Забыли отметить уход, Часов за месяц. Каждая плитка открывает своё окно (?v=…), «‹ Назад» с открытой
плиткой ведёт к таблице (data-menu-base). Одинаково у админа, директора, зама и бухгалтера."""
import re

from .conftest import login

ROW1 = ["Сейчас на месте", "Ушли", "Не отмечались", "Для сведения"]
ROW2 = ["Нужно решить", "Забыли отметить уход", "Часов за месяц"]
VIEWS = {"on": "Сейчас на месте", "out": "Сегодня уже ушли", "none": "Сегодня не отмечались",
         "open": "Забыли отметить уход", "hours": "Часов за месяц", "boss": "", "info": ""}


def _rows(html):
    rows = re.findall(r'<div class="pult-tiles vs-tiles ord-tiles"[^>]*>(.*?)</div>', html, re.S)
    return [re.findall(r'<span class="ptile-t">([^<]+)</span>', r) for r in rows]


def test_timesheet_tiles_all_open(client):
    for who in ("admin", "director", "deputy", "buh"):
        login(client, who, who + "123")
        page = client.get("/timesheet").text
        rows = _rows(page)
        # 26.11.2026 (брат): бухгалтер ничего не решает — без «Нужно решить» (его дело — «Забыли отметить уход»)
        row2 = ROW2[1:] if who == "buh" else ROW2
        assert rows[0] == ROW1 and rows[1] == row2, (who, rows)
        assert 'data-menu-base="/timesheet"' in page
        assert 'class="table ts"' in page                       # без плитки — обычная таблица
        for v, head in VIEWS.items():
            if who == "buh" and v == "boss":
                continue
            r = client.get(f"/timesheet?v={v}")
            assert r.status_code == 200, (who, v)
            assert 'class="table ts"' not in r.text              # окно вместо таблицы
            assert re.search(r'class="ptile[^"]*\bon\b', r.text), (who, v)   # выбранная плитка — «Назад» к таблице
            if head:
                assert f'<span class="headline">{head}' in r.text, (who, v)
        client.post("/logout")


def test_timesheet_own_rows_for_staff(client):
    """У сотрудника без «всех» — свой табель, плитки дел только когда есть; «Назад» — к своему табелю."""
    login(client, "andrey", "andrey123")
    page = client.get("/timesheet").text
    assert "Сейчас на месте" not in page and "Часов за месяц" in page
    client.post("/logout")
