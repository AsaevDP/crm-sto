"""25.11.2026 (брат): фотоархив (фото нарядов старше года удаляются сами, закреплённое фото машины — никогда,
удаляет вручную только админ) и архив клиентов (Excel + JSON) с восстановлением в CRM."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.db import SessionLocal, engine
from app.models import AuditLog, Branch, Client, Vehicle, WoPhoto, WorkOrder, WoWork
from app.services import client_archive as ca
from app.services import photo_archive as pa
from app.services.orders import PHOTO_DIR

from .conftest import login
from .test_step3 import _create


def _as(c, who):
    c.post("/logout")
    login(c, who, who + "123")


def _wo(url):
    with SessionLocal() as db:
        return db.get(WorkOrder, uuid.UUID(url.rsplit("/", 1)[1]))


def test_photo_archive_cleanup_and_rights(client):
    _as(client, "irina")
    url = _create(client, scenario="B", photos=3, mechanic_id="", plate="КА 2512 АВ", phone="0632512001").json()["url"]
    wo = _wo(url)
    with SessionLocal() as db:
        ps = db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id)).all()
        files = [PHOTO_DIR / p.file for p in ps]
    client.post(f"/vehicles/{wo.vehicle_id}/photo", data={"photo_id": str(ps[0].id)})
    # Менеджер фотоархив не видит
    assert client.get("/settings/photos").status_code == 403
    # Наряд закрыт год назад — фото старые
    with SessionLocal() as db:
        db.execute(text("UPDATE work_orders SET status='closed' WHERE id=:i"), {"i": wo.id.hex})
        old = datetime.now(timezone.utc) - timedelta(days=400)
        for p in db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo.id)):
            p.created_at = old
        db.commit()
    _as(client, "admin")
    page = client.get("/settings/photos")
    assert page.status_code == 200 and "Фотоархив" in page.text and wo.number in page.text
    assert "Удалить старые сейчас" in page.text
    assert "Фотоархив" in client.get("/settings").text and "Архив клиентов" in client.get("/settings").text
    with SessionLocal() as db:
        bid = db.scalar(select(Branch.id))
        assert pa.summary(db, bid)["old"] >= 3
        n = pa.cleanup(db, bid)
        assert n >= 3
        assert db.scalar(select(func.count()).select_from(WoPhoto).where(WoPhoto.work_order_id == wo.id)) == 0
        v = db.get(Vehicle, wo.vehicle_id)
        assert v.photo and (PHOTO_DIR / v.photo).exists()   # закреплённое фото машины осталось
    assert not any(f.exists() for f in files)
    # Открытый наряд со старыми фото — не трогаем
    _as(client, "irina")
    url2 = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2512 АС", phone="0632512002").json()["url"]
    wo2 = _wo(url2)
    with SessionLocal() as db:
        for p in db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo2.id)):
            p.created_at = datetime.now(timezone.utc) - timedelta(days=400)
        db.commit()
        pa.cleanup(db, bid)
        assert db.scalar(select(func.count()).select_from(WoPhoto).where(WoPhoto.work_order_id == wo2.id)) == 1
    # Удалить вручную — только админ
    with SessionLocal() as db:
        pid = db.scalar(select(WoPhoto.id).where(WoPhoto.work_order_id == wo2.id))
    assert client.post("/settings/photos/delete", data={"kind": "wo", "pid": str(pid)}).status_code == 403
    _as(client, "director")
    assert client.post("/settings/photos/delete", data={"kind": "wo", "pid": str(pid)}).status_code == 403
    _as(client, "admin")
    client.post("/settings/photos/delete", data={"kind": "order", "pid": str(wo2.id)})
    with SessionLocal() as db:
        assert db.get(WoPhoto, pid) is None
    # Срок хранения меняет админ
    client.post("/settings/photos/keep", data={"keep_days": "500"})
    assert "500 дн." in client.get("/settings/photos").text


def test_client_archive_export_and_restore(client):
    _as(client, "irina")
    url = _create(client, scenario="B", photos=1, mechanic_id="", plate="КА 2513 АВ", phone="0632513001",
                  name="Архивный Клиент").json()["url"]
    wo = _wo(url)
    with SessionLocal() as db:
        c = db.get(Client, wo.client_id)
        cid, code, bid = c.id, c.code, c.branch_id
        n_audit = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.record_id == str(wo.id)))
    _as(client, "admin")
    assert client.get("/settings/archive").status_code == 200
    client.post("/settings/archive/run")
    with SessionLocal() as db:
        base = ca.root(db, bid)
    folders = list((base / "clients").glob(f"{code} *"))
    assert len(folders) == 1
    folder = folders[0]
    data = json.loads((folder / ca.JSON_NAME).read_text(encoding="utf-8"))
    assert data["format"] == ca.FORMAT and data["tables"]["work_orders"][0]["number"] == wo.number
    assert data["tables"]["audit_log"]   # история наряда тоже в архиве
    from openpyxl import load_workbook
    wb = load_workbook(folder / ca.XLSX_NAME)
    assert {"Карточка", "Машины", "Визиты", "Работы", "Запчасти", "Оплаты", "История нарядов"} <= set(wb.sheetnames)
    assert wb["Визиты"]["B2"].value == wo.number
    assert (base / "_Список клиентов.xlsx").exists() and (base / "КАК ВОССТАНОВИТЬ.txt").exists()
    # Повторно без изменений — файл не переписывается
    mtime = (folder / ca.JSON_NAME).stat().st_mtime
    with SessionLocal() as db:
        r = ca.export_all(db, bid)
    assert (folder / ca.JSON_NAME).stat().st_mtime == mtime and r["changed"] == 0
    # Проверка: всё уже есть — добавится 0
    with SessionLocal() as db:
        docs, errs = ca.read_docs(base)
        r = ca.restore(db, docs, bid, None, dry=True)
        assert sum(r["stats"].values()) == 0, (r, errs)
    r = client.post("/settings/archive/restore", data={"folder": ""}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    page = client.get("/settings/archive").text
    assert "добавлять нечего" in page, page[page.find('id="restore"'):][:1500]
    # «CRM сломалась»: клиента и его наряд стёрли из базы
    with engine.connect() as con:
        con.exec_driver_sql("PRAGMA foreign_keys=OFF")
        ids = {"w": wo.id.hex, "c": cid.hex, "v": wo.vehicle_id.hex}
        for t in ("wo_works", "wo_parts", "wo_findings", "wo_photos", "wo_status_history", "payments", "order_masters"):
            con.execute(text(f"DELETE FROM {t} WHERE work_order_id=:w"), ids)
        con.execute(text("DELETE FROM audit_log WHERE record_id=:s"), {"s": str(wo.id)})
        con.execute(text("DELETE FROM work_orders WHERE id=:w"), ids)
        con.execute(text("DELETE FROM client_events WHERE client_id=:c"), ids)
        con.execute(text("DELETE FROM client_phones WHERE client_id=:c"), ids)
        con.execute(text("DELETE FROM vehicles WHERE id=:v"), ids)
        con.execute(text("DELETE FROM clients WHERE id=:c"), ids)
        con.commit()
    with SessionLocal() as db:
        assert db.get(Client, cid) is None
    client.post("/settings/archive/restore", data={"folder": str(base)}, follow_redirects=False)
    page = client.get("/settings/archive").text
    assert "Восстановить" in page and "клиентов <b>1</b>" in page
    client.post("/settings/archive/restore", data={"folder": str(base), "go": "1"})
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c is not None and c.full_name == "Архивный Клиент" and c.code == code
        w = db.get(WorkOrder, wo.id)
        assert w is not None and w.number == wo.number and w.client_id == cid
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.record_id == str(wo.id))) == n_audit
    # Карта клиента снова открывается с историей
    assert wo.number in client.get(f"/clients/{cid}").text
    # Повторное восстановление — ничего не дублирует
    with SessionLocal() as db:
        docs, _ = ca.read_docs(base)
        r = ca.restore(db, docs, bid, None, dry=True)
        assert sum(r["stats"].values()) == 0
    # Менеджеру — нельзя
    _as(client, "irina")
    assert client.get("/settings/archive").status_code == 403
