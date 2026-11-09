"""Кабинет (10.11.2026): разделы СТО / Разбор, отчёты «Выполненные работы», «Проданные запчасти», «Продажи б/у»,
«Доноры»; документ для печати с шаблоном шапки и подписей; Excel."""
import io

from .test_step6 import _as, _order_to_work


def test_cabinet_sections_docs_excel(client):
    _order_to_work(client, "AA5151CB", "0675151515", "Колодки (тест кабинета)")
    _as(client, "director")
    page = client.get("/reports").text
    assert "СТО" in page and "Разбор" in page and "Выполненные работы" in page and "Проданные запчасти" in page
    assert "Продажи б/у" in client.get("/reports?sec=razbor").text
    for tab in ("works", "parts", "razbor", "donors", "overview", "staff", "quarters", "accounting"):
        assert client.get(f"/reports?tab={tab}&period=year").status_code == 200, tab

    # Документ: шапка и подписи из шаблона, сохраняются как шаблон
    doc = client.get("/reports/doc?tab=works&period=year").text
    assert "ВЫПОЛНЕННЫЕ РАБОТЫ" in doc and "contenteditable" in doc and "Директор" in doc
    r = client.post("/reports/template", json={"header": "ФОП Тестов Т.Т.\nг. Киев", "footer": "Подвал теста",
                                               "signer1": "Руководитель", "signer2": "Кассир"})
    assert r.status_code == 200
    doc = client.get("/reports/doc?tab=parts&period=year").text
    assert "ФОП Тестов Т.Т." in doc and "Кассир" in doc and "Подвал теста" in doc

    x = client.get("/reports/export.xlsx?tab=works&period=year")
    assert x.status_code == 200 and "spreadsheetml" in x.headers["content-type"]
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(x.content)).active
    assert "Выполненные работы" in ws["A1"].value and ws["A3"].value == "Дата"

    # Слесарь в кабинет не попадает
    _as(client, "sergey")
    assert client.get("/reports").status_code == 403
