"""Панель дат (10.11.2026): одинаковая везде, ← → и быстрые периоды; новый Пульт."""
from datetime import date

from app.services import periods as pr

from .test_step6 import _as


def test_period_math():
    p = pr.parse("week", "2026-11-12")
    assert (p.d1, p.d2) == (date(2026, 11, 9), date(2026, 11, 15))
    q = pr.parse("quarter", "2026-11-12")
    assert (q.d1, q.d2) == (date(2026, 10, 1), date(2026, 12, 31)) and q.label == "4 квартал 2026"
    assert pr.shift("month", date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert pr.shift("quarter", date(2026, 11, 1), -1) == date(2026, 8, 1)
    c = pr.parse("custom", "", "2026-11-10", "2026-11-01")
    assert (c.d1, c.d2) == (date(2026, 11, 1), date(2026, 11, 10))
    b = pr.Bar(pr.parse("month", "2026-11-05"), "/x", {"tab": "a"})
    assert "d=2026-10-05" in b.prev and "d=2026-12-05" in b.next and "tab=a" in b.prev
    assert [k for k, *_ in b.quick()] == ["day", "week", "month", "quarter", "year"]


def test_period_bar_everywhere_and_pult(client):
    _as(client, "admin")
    pages = ["/reports?p=week", "/reports?tab=works&p=quarter", "/payroll?p=year", "/audit?p=month", "/money?p=week",
             "/timesheet?p=month&d=2026-10-01", "/schedule?p=week", "/orders?tab=all&p=month"]
    for url in pages:
        html = client.get(url).text
        assert "data-period-bar" in html and 'data-pkey="prev"' in html, url
    assert "Октябрь 2026" in client.get("/timesheet?p=month&d=2026-10-15").text
    assert "4 квартал" in client.get("/reports?p=quarter&d=2026-11-10").text
    assert "1 квартал 2026" in client.get("/reports?period=quarter&y=2026&n=1").text   # старые ссылки работают
    assert 'data-pkey="all"' in client.get("/orders").text                            # наряды: «Всё время»

    pult = client.get("/pult").text
    for t in ("На СТО", "Согласование", "Запчасти", "Готовы", "Запись сегодня", "На месте", "Деньги сегодня"):
        assert t in pult, t
    _as(client, "irina")
    assert "Деньги сегодня" in client.get("/pult").text   # менеджер видит кассу дня
