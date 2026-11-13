"""CRM СТО — точка входа. Запуск: run.bat (или uvicorn app.main:app --port 8810)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import audit  # noqa: F401 — подключает автоматический журнал
from .config import COOKIE_SECURE, DEMO, MEDIA_DIR, ROOT, SECRET_KEY
from .db import Base, add_missing_columns, engine
from .routes import (attendance, auth, diagnosis, folders, money, orders, pages, razbor, reports, schedule,
                     settings, stock, tools, wash)
from .seed import seed
from .web import Forbidden, NotLoggedIn, ViewOnly, flash, templates


@asynccontextmanager
async def lifespan(_app):
    # До шага 8 таблицы создаются напрямую. Перед боевой базой подключим Alembic-миграции.
    from .services import backup
    backup.on_start()   # копия базы ДО любых изменений при запуске (11.11.2026)
    Base.metadata.create_all(engine)
    add_missing_columns()
    from .db import ensure_unique_indexes
    ensure_unique_indexes()
    seed()
    from .db import SessionLocal
    from .services.stock import backfill_categories
    from .services.stock_cat import backfill_barcodes  # импорт включает и штрихкод у новых позиций
    with SessionLocal() as db:
        backfill_categories(db)
        backfill_barcodes(db)
        from .services.stock import fix_half_ordered
        fix_half_ordered(db)   # 13.11.2026: «Заказал» по строке — сразу в накладную у кладовщика
        from sqlalchemy import select
        from .models import Branch
        from .services import brands
        for b in db.scalars(select(Branch)).all():
            brands.seed(db, b.id)   # справочник брендов: стартовый список (11.11.2026)
            from .services import tools as tools_svc
            tools_svc.adopt_text_places(db, b.id)   # «Шкаф 1 · полка 2» текстом → шкафы и полки (11.11.2026)
            from .services import razbor as razbor_svc
            razbor_svc.seed_refs(db, b.id)          # справочники разбора: категории, марки и модели (11.11.2026)
        db.commit()
    if DEMO:
        from .demo_data import populate
        populate()  # демо: выдуманные клиенты, наряды на всех этапах, склад
        with SessionLocal() as db:   # после демо-доноров — марки и модели в справочник разбора
            from sqlalchemy import select
            from .models import Branch
            from .services import razbor as razbor_svc
            for b in db.scalars(select(Branch)).all():
                razbor_svc.seed_refs(db, b.id)
            db.commit()
    backup.start_daily()   # и раз в сутки, пока CRM работает
    yield


app = FastAPI(title="CRM СТО", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="crm_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=COOKIE_SECURE)

app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(orders.router)
app.include_router(diagnosis.router)
app.include_router(stock.router)
app.include_router(money.router)
app.include_router(tools.router)
app.include_router(razbor.router)
app.include_router(attendance.router)
app.include_router(folders.router)
app.include_router(schedule.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(wash.router)


@app.exception_handler(NotLoggedIn)
async def _not_logged_in(request: Request, _exc):
    if request.method != "GET" or request.headers.get("x-requested-with") == "fetch":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, _exc):
    return templates.TemplateResponse(request, "forbidden.html", {}, status_code=403)


def _problem(request: Request, text: str, code: int):
    """Понятный ответ вместо «Internal Server Error» (отчёт тестировщика 11.11.2026)."""
    from fastapi.responses import HTMLResponse, JSONResponse
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": False, "error": text, "errors": [text]}, status_code=code)
    back = request.headers.get("referer") or "/"
    from html import escape
    return HTMLResponse(
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<title>Не получилось</title><link rel="stylesheet" href="/static/css/app.css"></head>'
        f'<body style="padding:32px;font-family:-apple-system,Segoe UI,sans-serif"><h2>Не получилось</h2>'
        f'<p>{escape(text)}</p><p><a class="btn" href="{escape(back)}">← Назад</a></p></body></html>', status_code=code)


from sqlalchemy.exc import OperationalError  # noqa: E402


@app.exception_handler(OperationalError)
async def _db_busy(request: Request, exc: OperationalError):
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return _problem(request, "База сейчас занята — изменения не сохранены. Подождите пару секунд и нажмите ещё раз.", 503)
    return _problem(request, "Ошибка базы данных — изменения не сохранены. Сообщите админу, что вы делали.", 500)


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    import logging
    logging.getLogger("crm").exception("Ошибка на %s %s", request.method, request.url.path)
    return _problem(request, "Что-то пошло не так — изменения не сохранены. Проверьте введённые данные и попробуйте ещё раз.", 500)


@app.exception_handler(ViewOnly)
async def _view_only(request: Request, exc: ViewOnly):
    """«Глазами сотрудника»: это действие так нельзя (пароль / PIN) — возвращаем назад с пояснением."""
    from fastapi.responses import JSONResponse
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": False, "error": "Глазами сотрудника пароль и PIN менять нельзя"}, status_code=403)
    flash(request, "Глазами сотрудника пароль и PIN менять нельзя — вернитесь к себе", "error")
    return RedirectResponse(exc.back, status_code=303)
