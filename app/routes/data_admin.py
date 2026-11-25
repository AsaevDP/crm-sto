"""Данные владельца (25.11.2026, брат): «Фотоархив» и «Архив клиентов» в «Настройках».

Видит — право data.archive (по умолчанию только админ = владелец). Удаляет фото и меняет срок хранения — только админ.
"""
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form
from fastapi.responses import FileResponse, RedirectResponse

from ..models import WoPhoto, WorkOrder
from ..permissions import ADMIN_ROLE
from ..services import client_archive as ca
from ..services import photo_archive as pa
from ..services import settings as st
from ..web import Ctx, Forbidden, flash, get_ctx

router = APIRouter(prefix="/settings")
TABS = {"orders": "Наряды", "cars": "Машины", "razbor": "Склад и разбор", "tools": "Инструмент"}


def _to(url):
    return RedirectResponse(url, status_code=303)


def _admin(ctx: Ctx):
    ctx.need("data.archive")
    if ctx.user.role.code != ADMIN_ROLE:
        raise Forbidden()


# ---------- Фотоархив ----------
@router.get("/photos")
def photos(ctx: Ctx = Depends(get_ctx), tab: str = "orders", m: str = "", q: str = ""):
    ctx.need("data.archive")
    tab = tab if tab in TABS else "orders"
    b = ctx.branch.id
    data = {}
    if tab == "orders":
        data["groups"] = pa.order_groups(ctx.db, b, m, q)
        data["months"] = pa.months(ctx.db, b)
    elif tab == "cars":
        data["cars"] = pa.cars(ctx.db, b)
    elif tab == "razbor":
        data["files"] = pa.razbor_files(ctx.db, b)
    else:
        data["files"] = pa.tool_files(ctx.db, b)
    return ctx.render("settings/photos.html", title="Фотоархив", tab=tab, TABS=TABS, m=m, q=q,
                      sm=pa.summary(ctx.db, b), s=st.get(ctx.db, b, "photos"), KINDS=pa.KINDS,
                      is_admin=ctx.user.role.code == ADMIN_ROLE, STAGES={"intake": "Приёмка", "diag": "Диагностика",
                                                                          "work": "Работа"}, **data)


@router.get("/photos/file")
def photo_file(rel: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("data.archive")
    p = pa.path(rel)
    if p is None:
        raise Forbidden()
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


@router.post("/photos/keep")
def photos_keep(ctx: Ctx = Depends(get_ctx), keep_days: str = Form("365")):
    _admin(ctx)
    try:
        d = int(keep_days)
    except ValueError:
        d = 0
    if not 30 <= d <= 3650:
        flash(ctx.request, "Срок хранения — от 30 до 3650 дней", "error")
        return _to("/settings/photos")
    st.put(ctx.db, ctx.branch.id, "photos", {"keep_days": d})
    ctx.db.commit()
    flash(ctx.request, f"Фото нарядов хранятся {d} дн.")
    return _to("/settings/photos")


@router.post("/photos/cleanup")
def photos_cleanup(ctx: Ctx = Depends(get_ctx)):
    _admin(ctx)
    n = pa.cleanup(ctx.db, ctx.branch.id)
    flash(ctx.request, f"Удалено старых фото: {n}" if n else "Старых фото нет — удалять нечего")
    return _to("/settings/photos")


@router.post("/photos/delete")
def photos_delete(ctx: Ctx = Depends(get_ctx), kind: str = Form(""), pid: str = Form(""), back: str = Form("")):
    """Удалить вручную — только админ: одно фото наряда, все фото наряда или фото склада / разбора."""
    _admin(ctx)
    try:
        u = uuid.UUID(pid)
    except ValueError:
        raise Forbidden()
    n = 0
    if kind == "wo":
        p = ctx.db.get(WoPhoto, u)
        if p and p.branch_id == ctx.branch.id:
            pa.delete_wo_photo(ctx.db, p)
            n = 1
    elif kind == "order":
        wo = ctx.db.get(WorkOrder, u)
        if wo and wo.branch_id == ctx.branch.id:
            n = pa.delete_order_photos(ctx.db, wo.id)
    elif kind in ("donor_photos", "stock_photos", "site_new_photos"):
        n = 1 if pa.delete_razbor_photo(ctx.db, kind, u) else 0
    ctx.db.commit()
    flash(ctx.request, f"Удалено фото: {n}")
    return _to(back if back.startswith("/settings/photos") else "/settings/photos")


# ---------- Архив клиентов ----------
@router.get("/archive")
def archive(ctx: Ctx = Depends(get_ctx)):
    ctx.need("data.archive")
    from ..services import backup
    s = ca.status(ctx.db, ctx.branch.id)
    last = None
    if s.get("last_at"):
        try:
            last = datetime.fromisoformat(s["last_at"]).astimezone()
        except ValueError:
            pass
    return ctx.render("settings/client_archive.html", title="Архив клиентов", s=s, last=last, backups=backup.listing()[:5],
                      is_admin=ctx.user.role.code == ADMIN_ROLE, check=ctx.request.session.pop("arch_check", None))


@router.post("/archive/run")
def archive_run(ctx: Ctx = Depends(get_ctx)):
    _admin(ctx)
    r = ca.export_all(ctx.db, ctx.branch.id)
    flash(ctx.request, f"Архив обновлён: клиентов {r['clients']}, изменилось {r['changed']}")
    return _to("/settings/archive")


@router.post("/archive/folder")
def archive_folder(ctx: Ctx = Depends(get_ctx), folder: str = Form("")):
    _admin(ctx)
    f = folder.strip()
    if f:
        p = Path(f)
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / ".check").write_text("ok", encoding="utf-8")
            (p / ".check").unlink()
        except OSError:
            flash(ctx.request, "В эту папку нельзя записать — проверьте путь и доступ", "error")
            return _to("/settings/archive")
    st.put(ctx.db, ctx.branch.id, "client_archive", {"folder": f})
    ctx.db.commit()
    flash(ctx.request, "Папка архива сохранена" if f else "Папка архива — по умолчанию (data/archive)")
    return _to("/settings/archive")


@router.get("/archive/zip")
def archive_zip(ctx: Ctx = Depends(get_ctx)):
    _admin(ctx)
    p = ca.make_zip(ctx.db, ctx.branch.id)
    return FileResponse(p, filename=f"архив-клиентов-{datetime.now():%Y-%m-%d}.zip", media_type="application/zip")


@router.post("/archive/restore")
def archive_restore(ctx: Ctx = Depends(get_ctx), folder: str = Form(""), go: str = Form("")):
    """Проверить (сколько добавится) или восстановить из папки архива. Существующее не трогаем."""
    _admin(ctx)
    base = Path(folder.strip()) if folder.strip() else ca.root(ctx.db, ctx.branch.id)
    docs, errors = ca.read_docs(base)
    if not docs:
        flash(ctx.request, "; ".join(errors) or "В папке нет файлов «данные.json»", "error")
        return _to("/settings/archive")
    if go:
        from ..services import backup
        backup.make("before-restore")   # на всякий случай — копия базы до восстановления
        r = ca.restore(ctx.db, docs, ctx.branch.id, ctx.user.id)
        flash(ctx.request, f"Восстановлено: клиентов {r['stats']['clients']}, машин {r['stats']['vehicles']}, "
                           f"нарядов {r['stats']['work_orders']}" + (f", пропущено {r['skipped']}" if r["skipped"] else ""))
        return _to("/settings/archive")
    r = ca.restore(ctx.db, docs, ctx.branch.id, ctx.user.id, dry=True)
    ctx.db.rollback()
    ctx.request.session["arch_check"] = {"folder": folder.strip(), "docs": r["docs"], "errors": errors[:10],
                                         "clients": r["stats"]["clients"], "vehicles": r["stats"]["vehicles"],
                                         "orders": r["stats"]["work_orders"], "works": r["stats"]["wo_works"],
                                         "payments": r["stats"]["payments"], "skipped": r["skipped"]}
    return _to("/settings/archive#restore")
