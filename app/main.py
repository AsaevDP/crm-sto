"""CRM СТО — точка входа. Запуск: run.bat (или uvicorn app.main:app --port 8810)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import audit  # noqa: F401 — подключает автоматический журнал
from .config import COOKIE_SECURE, DEMO, MEDIA_DIR, ROOT, SECRET_KEY
from .db import Base, add_missing_columns, engine
from .routes import auth, diagnosis, orders, pages, settings, stock
from .seed import seed
from .web import Forbidden, NotLoggedIn, ViewOnly, flash, templates


@asynccontextmanager
async def lifespan(_app):
    # До шага 8 таблицы создаются напрямую. Перед боевой базой подключим Alembic-миграции.
    Base.metadata.create_all(engine)
    add_missing_columns()
    seed()
    if DEMO:
        from .demo_data import populate
        populate()  # демо: выдуманные клиенты, наряды на всех этапах, склад
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
app.include_router(settings.router)


@app.exception_handler(NotLoggedIn)
async def _not_logged_in(request: Request, _exc):
    if request.method != "GET" or request.headers.get("x-requested-with") == "fetch":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, _exc):
    return templates.TemplateResponse(request, "forbidden.html", {}, status_code=403)


@app.exception_handler(ViewOnly)
async def _view_only(request: Request, exc: ViewOnly):
    """В режиме «глазами сотрудника» действия выключены — возвращаем назад с пояснением."""
    from fastapi.responses import JSONResponse
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": False, "error": "Режим просмотра: изменения выключены"}, status_code=403)
    flash(request, "Режим просмотра глазами сотрудника — изменения выключены", "error")
    return RedirectResponse(exc.back, status_code=303)
