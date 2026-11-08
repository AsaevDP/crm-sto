"""Главная, разделы-заготовки (наполняются на следующих шагах), пульт, журнал."""
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import AuditLog, Lift, User
from ..web import Ctx, get_ctx

router = APIRouter()

# Что появится в разделе и на каком шаге — для пустых экранов
SECTIONS = {
    "/orders": ("Наряды", "orders", 3, "Приёмка с фото, два сценария (А/Б), назначение запчастиста, слесаря и подъёмника.",
                ["orders.view_all", "orders.view_own"]),
    "/stock": ("Склад", "stock", 5, "Номенклатура, ячейки, остатки, резервы и выдача по запросу.", ["stock.view"]),
    "/purchases": ("Закупки", "cart", 5, "Закупка под наряд и на склад одной накладной.", ["purchases.view"]),
    "/razbor": ("Разбор", "wrench", 7, "Б/у детали с донором и фото, экспорт для сайта.", ["razbor.view"]),
    "/money": ("Деньги", "money", 6, "Работы и цены, предоплата, журнал оплат, закрытие наряда.", ["money.view"]),
    "/timesheet": ("Табель", "clock", None, "Кто на месте, отметки по картам, часы за день и месяц.",
                   ["attendance.view_all", "attendance.view_own"]),
    "/payroll": ("Зарплата", "chart", None, "Начисления по дням: из каких нарядов и по какой ставке.",
                 ["payroll.view_own", "payroll.view_all"]),
}


@router.get("/")
def home(ctx: Ctx = Depends(get_ctx)):
    # Каждый попадает в первый доступный ему раздел
    target = ctx.menu[0]["href"] if ctx.menu else "/more"
    return RedirectResponse(target, status_code=303)


def _section(path: str):
    title, icon, step, text, need = SECTIONS[path]

    def view(ctx: Ctx = Depends(get_ctx)):
        ctx.need(*need)
        return ctx.render("section.html", title=title, icon_name=icon, step=step, text=text)
    return view


for _p in SECTIONS:
    router.add_api_route(_p, _section(_p), methods=["GET"])


@router.get("/pult")
def pult(ctx: Ctx = Depends(get_ctx)):
    ctx.need("dashboard.view")
    lifts = ctx.db.scalars(select(Lift).where(Lift.branch_id == ctx.branch.id, Lift.active.is_(True))
                           .order_by(Lift.number)).all()
    staff = ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True))
                           .order_by(User.full_name)).all()
    return ctx.render("pult.html", title="Пульт", lifts=lifts, staff=staff)


@router.get("/more")
def more(ctx: Ctx = Depends(get_ctx)):
    return ctx.render("more.html", title="Ещё")


TABLE_NAMES = {
    "users": "Сотрудник", "roles": "Роль", "role_permissions": "Право роли", "user_permissions": "Право сотрудника",
    "settings": "Настройки", "staff_cards": "Карта", "work_orders": "Наряд", "lifts": "Подъёмник",
}
ACTIONS = {"create": "создал", "update": "изменил", "delete": "удалил", "login": "вошёл", "logout": "вышел"}


@router.get("/audit")
def audit_view(ctx: Ctx = Depends(get_ctx)):
    ctx.need("audit.view")
    rows = ctx.db.scalars(select(AuditLog).where(AuditLog.branch_id == ctx.branch.id)
                          .order_by(AuditLog.created_at.desc()).limit(300)).all()
    users = {u.id: u for u in ctx.db.scalars(select(User))}
    return ctx.render("audit.html", title="Журнал", rows=rows, users=users, TABLE_NAMES=TABLE_NAMES, ACTIONS=ACTIONS)
