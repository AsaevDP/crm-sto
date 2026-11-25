"""Ночные дела CRM (25.11.2026, брат): раз в сутки, после 2:00 ночи (или сразу, если CRM ночью была выключена и
прошло больше суток):
- фото нарядов старше срока хранения — удалить (photo_archive.cleanup; закреплённое фото машины — никогда);
- архив клиентов (Excel + JSON) — обновить (client_archive.export_all).
Отметка последнего запуска — файл data/nightly.txt.
"""
import logging
import threading
import time
from datetime import datetime

from ..config import DATA_DIR

log = logging.getLogger("crm.nightly")
MARK = DATA_DIR / "nightly.txt"
HOUR = 2
_started = False


def _last() -> datetime | None:
    try:
        return datetime.fromisoformat(MARK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def due(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    last = _last()
    if last is None:
        return True
    if (now - last).total_seconds() >= 26 * 3600:   # ночью CRM была выключена — догоняем
        return True
    return now.date() > last.date() and now.hour >= HOUR


def run_now() -> dict:
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import Branch
    from . import client_archive, photo_archive
    out = {}
    with SessionLocal() as db:
        for b in db.scalars(select(Branch)).all():
            try:
                out["photos_removed"] = out.get("photos_removed", 0) + photo_archive.cleanup(db, b.id)
            except Exception:
                db.rollback()
                log.exception("Не удалось почистить старые фото")
            try:
                r = client_archive.export_all(db, b.id)
                out["clients"] = out.get("clients", 0) + r["clients"]
            except Exception:
                db.rollback()
                log.exception("Не удалось обновить архив клиентов")
    MARK.write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")
    return out


def start():
    """Фоновый поток: раз в 20 минут проверяем, пора ли."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(300)   # не мешаем запуску CRM
        while True:
            try:
                if due():
                    run_now()
            except Exception:
                log.exception("Ночные дела не выполнены")
            time.sleep(1200)

    threading.Thread(target=loop, name="crm-nightly", daemon=True).start()
