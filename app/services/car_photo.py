"""Фото машины в карте клиента (25.11.2026, брат).

Выбирают одно из фото, сделанных по нарядам этой машины (приёмка, диагностика, работа). Закреплённое фото
КОПИРУЕТСЯ в data/photos/cars/ — оно хранится всегда, даже когда фото нарядов удалятся через год (photo_archive).
"""
import secrets
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Vehicle, WoPhoto, WorkOrder
from .orders import PHOTO_DIR

CARS = "cars"
STAGES = {"intake": "Приёмка", "diag": "Диагностика", "work": "Работа"}


def choices(db: Session, v: Vehicle, limit: int = 60) -> list[dict]:
    """Фото нарядов этой машины — новые сверху (приёмка первой в каждом наряде)."""
    rows = db.execute(select(WoPhoto, WorkOrder).join(WorkOrder, WorkOrder.id == WoPhoto.work_order_id)
                      .where(WorkOrder.vehicle_id == v.id)
                      .order_by(WorkOrder.created_at.desc(), WoPhoto.created_at)).all()
    out = []
    for p, wo in rows[:limit]:
        if (PHOTO_DIR / p.file).exists():
            out.append({"p": p, "wo": wo, "stage": STAGES.get(p.stage, "")})
    return out


def _copy(rel: str, name: str) -> str:
    src = PHOTO_DIR / rel
    if not rel or not src.exists():
        return ""
    folder = PHOTO_DIR / CARS
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{name}{src.suffix.lower()}"
    shutil.copyfile(src, dst)
    return f"{CARS}/{dst.name}"


def _drop(v: Vehicle):
    for rel in (v.photo, v.photo_thumb):
        if rel and rel.startswith(CARS + "/"):
            try:
                (PHOTO_DIR / rel).unlink()
            except OSError:
                pass


def pin(db: Session, v: Vehicle, p: WoPhoto) -> str | None:
    """Закрепить фото. Возвращает текст ошибки или None."""
    wo = db.get(WorkOrder, p.work_order_id)
    if wo is None or wo.vehicle_id != v.id:
        return "Это фото не этой машины"
    name = f"{v.id.hex[:12]}-{secrets.token_hex(4)}"
    full = _copy(p.file, name)
    if not full:
        return "Файл фото не найден"
    thumb = _copy(p.thumb, name + "_t") if p.thumb else ""
    _drop(v)
    v.photo, v.photo_thumb = full, thumb
    return None


def clear(v: Vehicle):
    _drop(v)
    v.photo, v.photo_thumb = "", ""


def path(v: Vehicle, thumb: bool = False):
    rel = (v.photo_thumb if thumb and v.photo_thumb else v.photo) or ""
    if not rel:
        return None
    p = (PHOTO_DIR / rel).resolve()
    if PHOTO_DIR.resolve() not in p.parents or not p.exists():
        return None
    return p
