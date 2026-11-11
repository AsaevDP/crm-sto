"""Резервные копии базы (11.11.2026, отчёт тестировщика: «резервных копий нет»).

Как делаем: встроенный в SQLite способ backup — он правильно забирает и то, что ещё лежит в crm.db-wal.
Просто скопировать файл crm.db при работающей CRM нельзя — теряются последние записи.

Когда: при каждом запуске CRM (до любых изменений) и раз в сутки, пока CRM работает.
Куда: data/backups/crm-ГГГГ-ММ-ДД_ЧЧ-ММ.db. Храним последние KEEP копий, старые удаляются сами.
Восстановить: остановить CRM → переименовать нужную копию в data/crm.db → запустить (см. docs/backup.md).
Сервер, облако и копии на другой диск — шаг 8.
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR, DATABASE_URL

KEEP = 14
DAY = 24 * 3600
log = logging.getLogger("crm.backup")


def _db_path() -> Path | None:
    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    return Path(DATABASE_URL.removeprefix("sqlite:///"))


def folder() -> Path:
    return DATA_DIR / "backups"


def make(note: str = "") -> Path | None:
    """Сделать копию сейчас. Возвращает путь к копии (None — базы ещё нет или не SQLite)."""
    src = _db_path()
    if not src or not src.exists():
        return None
    folder().mkdir(parents=True, exist_ok=True)
    name = f"crm-{datetime.now():%Y-%m-%d_%H-%M-%S}{('-' + note) if note else ''}.db"
    dst = folder() / name
    con = sqlite3.connect(str(src), timeout=30)
    try:
        out = sqlite3.connect(str(dst))
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()
    _prune()
    return dst


def _prune():
    files = sorted(folder().glob("crm-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def listing() -> list[dict]:
    if not folder().exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "at": datetime.fromtimestamp(p.stat().st_mtime)}
            for p in sorted(folder().glob("crm-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)]


def last_time() -> float:
    files = list(folder().glob("crm-*.db")) if folder().exists() else []
    return max((p.stat().st_mtime for p in files), default=0)


def check(path: Path) -> bool:
    """Копия целая? (PRAGMA integrity_check)"""
    con = sqlite3.connect(str(path))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def on_start():
    try:
        make("start")
    except Exception:   # копия не должна мешать запуску CRM
        log.exception("Не удалась копия базы при запуске")


_started = False


def start_daily():
    """Фоновый поток: раз в час проверяем — прошли сутки с последней копии? — делаем новую."""
    global _started
    if _started or _db_path() is None:
        return
    _started = True

    def loop():
        while True:
            time.sleep(3600)
            try:
                if time.time() - last_time() >= DAY:
                    make("daily")
            except Exception:
                log.exception("Не удалась ежедневная копия базы")

    threading.Thread(target=loop, name="crm-backup", daemon=True).start()
