"""Главная, разделы-заготовки (наполняются на следующих шагах), пульт, журнал."""
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..models import AuditLog, Lift, User
from ..web import Ctx, get_ctx

router = APIRouter()

# Что появится в разделе и на каком шаге — для пустых экранов
SECTIONS = {
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
    from sqlalchemy import func
    from ..models import IssueRequest, Vehicle, WorkOrder
    from ..services.orders import fmt_plate, lifts_with_load
    db, bid = ctx.db, ctx.branch.id
    lifts = lifts_with_load(db, bid)
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([wo.vehicle_id for _, wo in lifts if wo])))}
    staff = db.scalars(select(User).where(User.branch_id == bid, User.active.is_(True)).order_by(User.full_name)).all()
    people = {u.id: u for u in staff}
    counts = dict(db.execute(select(WorkOrder.status, func.count()).where(WorkOrder.branch_id == bid)
                             .group_by(WorkOrder.status)).all())
    issue_new = db.scalar(select(func.count()).select_from(IssueRequest)
                          .where(IssueRequest.branch_id == bid, IssueRequest.status == "new"))
    # Чем занят каждый: активные наряды, где он назначен
    busy = {}
    from ..models.orders import WO_ACTIVE
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(WO_ACTIVE))):
        for uid in (wo.mechanic_id, wo.partsman_id):
            if uid:
                busy[uid] = busy.get(uid, 0) + 1
    from ..models import WoWorkSession
    paused = {s.work_order_id: s.pause_reason for s in db.scalars(select(WoWorkSession).where(
        WoWorkSession.branch_id == bid, WoWorkSession.kind == "pause", WoWorkSession.ended_at.is_(None)))}
    from ..services.attendance import _aware, local_tz, on_site_now
    presence = {uid: (e.direction, _aware(e.at).astimezone(local_tz()).strftime("%H:%M")) if e else None
                for uid, e in on_site_now(db, bid, staff).items()}
    return ctx.render("pult.html", title="Пульт", lifts=lifts, vehicles=vehicles, people=people, staff=staff, paused=paused,
                      presence=presence,
                      counts=counts, issue_new=issue_new, busy=busy, fmt_plate=fmt_plate)


@router.get("/more")
def more(ctx: Ctx = Depends(get_ctx)):
    return ctx.render("more.html", title="Ещё")


TABLE_NAMES = {
    "users": "Сотрудник", "roles": "Роль", "role_permissions": "Право роли", "user_permissions": "Право сотрудника",
    "settings": "Настройки", "staff_cards": "Карта", "work_orders": "Наряд", "lifts": "Подъёмник",
    "clients": "Клиент", "vehicles": "Авто", "wo_photos": "Фото", "wo_works": "Работа", "wo_status_history": "Статус наряда",
    "order_counters": "Счётчик номеров", "wo_findings": "Находка", "wo_parts": "Запчасть", "payments": "Оплата",
}
# Названия полей в журнале — по-русски
FIELD_NAMES = {
    "full_name": "ФИО", "name": "Название", "login": "Логин", "phone": "Телефон", "active": "Активен",
    "role_id": "Роль", "permission": "Право", "allow": "Разрешено", "key": "Настройка", "value": "Значение",
    "uid": "Номер карты", "label": "Подпись", "color": "Цвет", "password_hash": "Пароль", "pin_hash": "PIN",
    "number": "Номер", "status": "Статус", "scenario": "Сценарий", "client_id": "Клиент", "vehicle_id": "Авто",
    "manager_id": "Менеджер", "partsman_id": "Запчастист", "mechanic_id": "Слесарь", "lift_id": "Подъёмник",
    "complaint": "Жалоба", "plate": "Госномер", "make": "Марка", "model": "Модель", "year": "Год", "vin": "VIN",
    "mileage": "Пробег", "mileage_in": "Пробег на приёмке", "cancel_reason": "Причина отмены",
    "fuel_level": "Топливо", "intake_items": "В машине", "intake_note": "Замечания", "to_status": "Новый статус",
    "from_status": "Был статус", "comment": "Комментарий", "code": "Код", "last": "Последний номер", "year_": "Год",
    "file": "Файл", "stage": "Этап", "notes": "Заметки", "work_order_id": "Наряд",
}
# Какие поля показывать в журнале при создании записи (остальное — служебное)
CREATE_FIELDS = ("full_name", "name", "login", "permission", "key", "uid", "number", "plate", "make", "model",
                 "phone", "to_status", "stage")
ACTIONS = {"create": "создал", "update": "изменил", "delete": "удалил", "login": "вошёл", "logout": "вышел",
           "view_as": "смотрел глазами", "view_as_stop": "вернулся к себе"}


@router.get("/audit")
def audit_view(ctx: Ctx = Depends(get_ctx)):
    ctx.need("audit.view")
    rows = ctx.db.scalars(select(AuditLog).where(AuditLog.branch_id == ctx.branch.id)
                          .order_by(AuditLog.created_at.desc()).limit(300)).all()
    users = {u.id: u for u in ctx.db.scalars(select(User))}
    return ctx.render("audit.html", title="Журнал", rows=rows, users=users, TABLE_NAMES=TABLE_NAMES, ACTIONS=ACTIONS,
                      FIELD_NAMES=FIELD_NAMES, CREATE_FIELDS=CREATE_FIELDS)
