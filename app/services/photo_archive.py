"""Фотоархив (25.11.2026, брат): все фото CRM в одном месте, сколько места занято, удаление.

Где лежат: data/photos/ (на сервере — общая папка для всех компьютеров).
- <id наряда>/…      — фото нарядов (приёмка, диагностика, работа): WoPhoto. **Удаляются сами через keep_days**
                        (настройка «photos», по умолчанию 365 дней) — только у закрытых и отменённых нарядов.
- cars/…             — закреплённое фото машины в карте клиента (Vehicle.photo). **Хранится всегда.**
- razbor/…           — фото машин-доноров, деталей склада и разбора, «На сайт» (DonorPhoto, StockPhoto, SiteNewPhoto):
                        пока деталь / машина в CRM, фото нужно — само не удаляется.
- tools/…            — фото инструмента (Tool.photo): само не удаляется.
Удалить вручную — только админ (владелец), каждое удаление — в журнале.
"""
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import current_branch_id
from ..models import Client, DonorPhoto, SiteNewPhoto, StockPhoto, Tool, Vehicle, WoPhoto, WorkOrder
from . import settings as st
from .orders import PHOTO_DIR

CLOSED = ("closed", "cancelled")
KINDS = {"orders": "Фото нарядов", "cars": "Фото машин (закреплённые)", "razbor": "Склад, разбор, сайт",
         "tools": "Инструмент"}


def _size(rel: str) -> int:
    if not rel:
        return 0
    try:
        return (PHOTO_DIR / rel).stat().st_size
    except OSError:
        return 0


def _unlink(rel: str):
    if not rel:
        return
    p = (PHOTO_DIR / rel).resolve()
    if PHOTO_DIR.resolve() in p.parents:
        try:
            p.unlink()
        except OSError:
            pass


def _folder_size(sub: str) -> tuple[int, int]:
    root = PHOTO_DIR / sub if sub else PHOTO_DIR
    n = size = 0
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                n += 1
                size += f.stat().st_size
    return n, size


def summary(db: Session, branch_id) -> dict:
    """Сколько фото и сколько места: всего на диске и по видам."""
    n_all, size_all = _folder_size("")
    n_cars, size_cars = _folder_size("cars")
    n_rz, size_rz = _folder_size("razbor")
    n_tl, size_tl = _folder_size("tools")
    n_wo = db.scalar(select(func.count()).select_from(WoPhoto).where(WoPhoto.branch_id == branch_id)) or 0
    size_wo = max(size_all - size_cars - size_rz - size_tl, 0)
    keep = int(st.get(db, branch_id, "photos").get("keep_days") or 365)
    border = datetime.now(timezone.utc) - timedelta(days=keep)
    old = db.scalar(select(func.count()).select_from(WoPhoto).join(WorkOrder, WorkOrder.id == WoPhoto.work_order_id)
                    .where(WoPhoto.branch_id == branch_id, WoPhoto.created_at < border,
                           WorkOrder.status.in_(CLOSED))) or 0
    oldest = db.scalar(select(func.min(WoPhoto.created_at)).where(WoPhoto.branch_id == branch_id))
    return {"n": n_all, "size": size_all, "keep_days": keep, "old": old, "oldest": oldest,
            "by": {"orders": (n_wo, size_wo), "cars": (n_cars, size_cars), "razbor": (n_rz, size_rz),
                   "tools": (n_tl, size_tl)}}


def months(db: Session, branch_id) -> list[str]:
    """Месяцы, в которых есть фото нарядов (ГГГГ-ММ), новые сверху."""
    rows = db.scalars(select(WoPhoto.created_at).where(WoPhoto.branch_id == branch_id)).all()
    return sorted({r.astimezone().strftime("%Y-%m") for r in rows if r}, reverse=True)


def order_groups(db: Session, branch_id, month: str = "", q: str = "") -> list[dict]:
    """Фото нарядов по нарядам: наряд, клиент, машина, сколько фото и места. Новые сверху."""
    qy = (select(WoPhoto, WorkOrder).join(WorkOrder, WorkOrder.id == WoPhoto.work_order_id)
          .where(WoPhoto.branch_id == branch_id).order_by(WorkOrder.created_at.desc(), WoPhoto.created_at))
    rows = db.execute(qy).all()
    if month:
        rows = [(p, wo) for p, wo in rows if p.created_at.astimezone().strftime("%Y-%m") == month]
    groups: dict = {}
    for p, wo in rows:
        g = groups.setdefault(wo.id, {"wo": wo, "photos": [], "size": 0})
        g["photos"].append(p)
        g["size"] += _size(p.file) + _size(p.thumb)
    if not groups:
        return []
    vs = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([g["wo"].vehicle_id for g in groups.values()])))}
    cs = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_([g["wo"].client_id for g in groups.values()])))}
    out = []
    ql = q.strip().lower()
    for g in groups.values():
        g["v"], g["c"] = vs.get(g["wo"].vehicle_id), cs.get(g["wo"].client_id)
        if ql:
            hay = " ".join([g["wo"].number, g["c"].full_name if g["c"] else "", g["v"].plate if g["v"] else "",
                            f'{g["v"].make} {g["v"].model}' if g["v"] else ""]).lower().replace(" ", "")
            if ql.replace(" ", "") not in hay:
                continue
        out.append(g)
    return out


def cars(db: Session, branch_id) -> list[Vehicle]:
    return db.scalars(select(Vehicle).where(Vehicle.branch_id == branch_id, Vehicle.photo != "")
                      .order_by(Vehicle.updated_at.desc())).all()


def razbor_files(db: Session, branch_id) -> list[dict]:
    out = []
    for model, label in ((DonorPhoto, "Машина-донор"), (StockPhoto, "Деталь склада / разбора"), (SiteNewPhoto, "На сайт")):
        for p in db.scalars(select(model).where(model.branch_id == branch_id).order_by(model.created_at.desc())):
            out.append({"kind": model.__tablename__, "id": p.id, "file": p.file, "label": label, "at": p.created_at,
                        "size": _size(p.file)})
    return out


def tool_files(db: Session, branch_id) -> list[dict]:
    return [{"id": t.id, "file": t.photo, "label": f"{t.inv_no} · {t.name}", "at": t.updated_at}
            for t in db.scalars(select(Tool).where(Tool.branch_id == branch_id, Tool.photo != "")
                                .order_by(Tool.name))]


# ---------- Удаление ----------
def delete_wo_photo(db: Session, p: WoPhoto):
    """Удалить фото наряда (файл, миниатюру и запись). Закреплённое фото машины — отдельная копия, не трогаем."""
    _unlink(p.file)
    _unlink(p.thumb)
    db.delete(p)


def delete_order_photos(db: Session, wo_id) -> int:
    n = 0
    for p in db.scalars(select(WoPhoto).where(WoPhoto.work_order_id == wo_id)).all():
        delete_wo_photo(db, p)
        n += 1
    folder = PHOTO_DIR / str(wo_id)
    if folder.exists() and not any(folder.iterdir()):
        shutil.rmtree(folder, ignore_errors=True)
    return n


def delete_razbor_photo(db: Session, kind: str, pid) -> bool:
    model = {"donor_photos": DonorPhoto, "stock_photos": StockPhoto, "site_new_photos": SiteNewPhoto}.get(kind)
    p = db.get(model, pid) if model else None
    if p is None:
        return False
    _unlink(p.file)
    db.delete(p)
    return True


def cleanup(db: Session, branch_id, today: datetime | None = None) -> int:
    """Удалить фото нарядов старше keep_days (только закрытые / отменённые наряды). Возвращает, сколько удалено."""
    s = st.get(db, branch_id, "photos")
    keep = int(s.get("keep_days") or 365)
    now = today or datetime.now(timezone.utc)
    border = now - timedelta(days=keep)
    tok = current_branch_id.set(branch_id)   # журнал без запроса — филиал из фото
    try:
        rows = db.scalars(select(WoPhoto).join(WorkOrder, WorkOrder.id == WoPhoto.work_order_id)
                          .where(WoPhoto.branch_id == branch_id, WoPhoto.created_at < border,
                                 WorkOrder.status.in_(CLOSED))).all()
        wos = set()
        for p in rows:
            wos.add(p.work_order_id)
            delete_wo_photo(db, p)
        st.put(db, branch_id, "photos", {"last_cleanup": now.isoformat(timespec="minutes"), "last_removed": len(rows)})
        db.commit()
    finally:
        current_branch_id.reset(tok)
    for w in wos:
        folder = PHOTO_DIR / str(w)
        if folder.exists() and not any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=True)
    return len(rows)


def path(rel: str):
    p = (PHOTO_DIR / rel).resolve()
    if PHOTO_DIR.resolve() not in p.parents or not p.exists() or not p.is_file():
        return None
    return p
