"""Журнал действий для людей: фильтры (сотрудник, раздел, действие, период) и понятные карточки
«что сделал → с чем → было → стало» вместо служебных кодов. Пишет журнал app/audit.py, тут — только чтение."""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..db import Base
from ..models import AuditLog, User

# Разделы журнала: код → (название, таблицы)
SECTIONS = {
    "orders": ("Наряды", ("work_orders", "wo_works", "wo_parts", "wo_findings", "wo_photos", "wo_status_history",
                          "wo_work_sessions", "order_counters")),
    "clients": ("Клиенты", ("clients", "vehicles", "client_events")),
    "schedule": ("Запись", ("appointments",)),
    "stock": ("Склад и закупки", ("parts_catalog", "stock_items", "stock_movements", "stock_photos", "locations",
                                  "suppliers", "purchases", "purchase_lines", "issue_requests", "issue_request_lines")),
    "razbor": ("Разбор", ("donors", "donor_photos", "used_sales")),
    "money": ("Деньги и зарплата", ("payments", "payroll_accruals", "payroll_payouts", "pay_schemes",
                                     "pay_scheme_lines", "user_pay_schemes", "part_groups", "part_group_items")),
    "staff": ("Сотрудники и табель", ("users", "roles", "role_permissions", "user_permissions", "staff_cards",
                                       "attendance_events", "access_points")),
    "tools": ("Инструмент", ("tools", "tool_moves", "tool_checks", "tool_check_lines")),
    "settings": ("Настройки", ("settings", "lifts", "branches", "permissions")),
}
TABLE_SECTION = {t: k for k, (_, ts) in SECTIONS.items() for t in ts}

TABLE_NAMES = {
    "users": "Сотрудник", "roles": "Роль", "role_permissions": "Право роли", "user_permissions": "Право сотрудника",
    "settings": "Настройки", "staff_cards": "Карта", "work_orders": "Наряд", "lifts": "Подъёмник",
    "clients": "Клиент", "vehicles": "Авто", "wo_photos": "Фото", "wo_works": "Работа", "wo_status_history": "Статус наряда",
    "order_counters": "Счётчик номеров", "wo_findings": "Находка", "wo_parts": "Запчасть", "payments": "Оплата",
    "wo_work_sessions": "Таймер работы", "client_events": "Заметка клиента", "appointments": "Запись",
    "parts_catalog": "Справочник запчастей", "stock_items": "Позиция склада", "stock_movements": "Движение склада",
    "stock_photos": "Фото детали", "locations": "Ячейка", "suppliers": "Поставщик", "purchases": "Закупка",
    "purchase_lines": "Строка закупки", "issue_requests": "Запрос на выдачу", "issue_request_lines": "Строка выдачи",
    "donors": "Донор", "donor_photos": "Фото донора", "used_sales": "Продажа б/у",
    "payroll_accruals": "Начисление зарплаты", "payroll_payouts": "Выплата зарплаты", "pay_schemes": "Схема зарплаты",
    "pay_scheme_lines": "Строка схемы", "user_pay_schemes": "Схема сотрудника", "part_groups": "Группа запчастей",
    "part_group_items": "Деталь в группе", "attendance_events": "Отметка табеля", "access_points": "Проходная",
    "tools": "Инструмент", "tool_moves": "Выдача инструмента", "tool_checks": "Проверка инструмента",
    "tool_check_lines": "Строка проверки", "branches": "Филиал", "permissions": "Право",
}

FIELD_NAMES = {
    "full_name": "ФИО", "name": "Название", "login": "Логин", "phone": "Телефон", "active": "Активен",
    "role_id": "Роль", "permission": "Право", "allow": "Разрешено", "key": "Настройка", "value": "Значение",
    "uid": "Номер карты", "label": "Подпись", "color": "Цвет", "password_hash": "Пароль", "pin_hash": "PIN",
    "number": "Номер", "status": "Статус", "scenario": "Сценарий", "client_id": "Клиент", "vehicle_id": "Авто",
    "manager_id": "Менеджер", "partsman_id": "Запчастист", "mechanic_id": "Слесарь", "lift_id": "Подъёмник",
    "complaint": "Жалоба", "plate": "Госномер", "make": "Марка", "model": "Модель", "year": "Год", "vin": "VIN",
    "mileage": "Пробег", "mileage_in": "Пробег на приёмке", "cancel_reason": "Причина отмены",
    "fuel_level": "Топливо", "intake_items": "В машине", "intake_note": "Замечания", "to_status": "Новый статус",
    "from_status": "Был статус", "comment": "Комментарий", "code": "Код", "last": "Последний номер",
    "file": "Файл", "stage": "Этап", "notes": "Заметки", "work_order_id": "Наряд",
    "hired_on": "Принят на работу", "dismissed_on": "Уволен", "dismiss_reason": "Причина увольнения",
    "dismissed_by": "Кто уволил", "price": "Цена", "cost": "Закупка", "qty": "Кол-во", "amount": "Сумма",
    "hours": "Нормо-часы", "rate": "Ставка", "markup_pct": "Наценка, %", "discount_pct": "Скидка, %",
    "method": "Способ", "note": "Примечание", "text": "Текст", "urgency": "Срочность", "approval": "Согласование",
    "holder_id": "За кем числится", "inv_no": "Инв. №", "serial": "Серийный №", "description": "Описание",
    "start_at": "Время", "duration_min": "Минут", "work": "Работа", "client_name": "Клиент", "car": "Авто",
    "direction": "Направление", "at": "Когда", "kind": "Вид", "category": "Категория", "article": "Артикул",
    "brand": "Бренд", "paid_on": "Дата выплаты", "on_date": "Дата", "done_at": "Выполнено", "result": "Итог",
    "charge": "Удержание", "payment_type": "Оплата", "prepay_amount": "Предоплата", "handover_state": "Акт выдачи",
    "handover_defects": "Неисправности", "handover_advice": "Рекомендации", "pause_reason": "Причина паузы",
    "user_id": "Сотрудник", "supplier_id": "Поставщик", "location_id": "Ячейка", "способ": "Способ входа",
    "кто смотрит": "Кто смотрит", "глазами": "Глазами",
}
# Что показывать при создании записи (остальное — служебное)
CREATE_FIELDS = ("full_name", "name", "login", "permission", "key", "uid", "number", "plate", "make", "model",
                 "phone", "to_status", "stage", "text", "amount", "qty", "price", "inv_no", "client_name", "work",
                 "direction", "comment", "note", "category")
SKIP_SHOW = {"id", "branch_id", "created_by", "updated_at", "created_at"}

ACTIONS = {"create": "создал", "update": "изменил", "delete": "удалил", "login": "вошёл", "logout": "вышел",
           "view_as": "смотрел глазами", "view_as_stop": "вернулся к себе"}
ACTION_FILTERS = {"create": "Создание", "update": "Изменение", "delete": "Удаление", "login": "Входы и выходы"}
PERIODS = {"today": "Сегодня", "week": "7 дней", "month": "30 дней", "all": "Всё"}
PERIOD_KINDS = ("day", "week", "month", "quarter", "year", "custom", "all")
PAGE = 150


@dataclass
class Filters:
    user: str = ""        # id сотрудника, "system" или пусто — все
    section: str = ""
    action: str = ""
    period: str = "week"  # старые ссылки (today / week / month / all)
    f: str = ""           # своя дата с
    t: str = ""           # своя дата по
    limit: int = PAGE
    p: str = ""           # общая панель дат (10.11.2026)
    d: str = ""

    def per(self):
        from . import periods
        p = self.p or ("custom" if self.f and self.t else {"today": "day", "all": "all"}.get(self.period, self.period))
        return periods.parse(p, self.d, self.f, self.t, default="week", allowed=PERIOD_KINDS)

    def dates(self) -> tuple[date | None, date | None]:
        per = self.per()
        return per.d1, per.d2

    def keep(self) -> dict:
        return {"user": self.user, "section": self.section, "action": self.action}

    def query_string(self, **over) -> str:
        from urllib.parse import urlencode
        d = {**self.keep(), **self.per().params(), **over}
        return urlencode({k: v for k, v in d.items() if v not in ("", None)})


def _utc(d: date, end=False) -> datetime:
    from .attendance import local_tz
    return datetime.combine(d, time.max if end else time.min, tzinfo=local_tz()).astimezone(timezone.utc)


def base_query(bid, flt: Filters, with_user=True):
    q = select(AuditLog).where(AuditLog.branch_id == bid)
    d1, d2 = flt.dates()
    if d1:
        q = q.where(AuditLog.created_at >= _utc(d1))
    if d2:
        q = q.where(AuditLog.created_at <= _utc(d2, end=True))
    if flt.section in SECTIONS:
        q = q.where(AuditLog.table.in_(SECTIONS[flt.section][1]))
    if flt.action == "login":
        q = q.where(AuditLog.action.in_(("login", "logout", "view_as", "view_as_stop")))
    elif flt.action in ACTION_FILTERS:
        q = q.where(AuditLog.action == flt.action)
    login = AuditLog.action.in_(LOGIN_ACTIONS)
    if with_user and flt.user == "system":
        q = q.where(AuditLog.created_by.is_(None), ~login)
    elif with_user and flt.user:
        try:
            uid = uuid.UUID(flt.user)
            # Входы пишутся до того, как человек «вошёл», — их ищем по самому сотруднику
            q = q.where(or_(AuditLog.created_by == uid, and_(login, AuditLog.record_id == str(uid))))
        except ValueError:
            pass
    return q


LOGIN_ACTIONS = ("login", "logout", "view_as", "view_as_stop")


def actor_id(r: AuditLog):
    if r.created_by is None and r.action in LOGIN_ACTIONS:
        try:
            return uuid.UUID(r.record_id)
        except ValueError:
            return None
    return r.created_by


def counts_by_user(db: Session, bid, flt: Filters) -> dict:
    """Сколько действий у каждого сотрудника при текущих фильтрах (без фильтра по сотруднику)."""
    sub = base_query(bid, flt, with_user=False).subquery()
    out: dict = {}
    for created_by, record_id, action, n in db.execute(
            select(sub.c.created_by, sub.c.record_id, sub.c.action, func.count())
            .group_by(sub.c.created_by, sub.c.record_id, sub.c.action)):
        key = created_by
        if created_by is None and action in LOGIN_ACTIONS:
            try:
                key = uuid.UUID(record_id)
            except ValueError:
                key = None
        out[key] = out.get(key, 0) + n
    return out


def fetch(db: Session, bid, flt: Filters, limit: int | None = None) -> tuple[list[AuditLog], bool]:
    lim = limit or flt.limit
    rows = db.scalars(base_query(bid, flt).order_by(AuditLog.created_at.desc()).limit(lim + 1)).all()
    return rows[:lim], len(rows) > lim


# ---------- Понятные подписи ----------
def _models() -> dict:
    return {m.class_.__tablename__: m.class_ for m in Base.registry.mappers}


def _label_of(obj, orders: dict) -> str:
    for attr in ("number", "full_name", "plate", "inv_no", "name", "code", "client_name", "login", "label"):
        v = getattr(obj, attr, None)
        if v:
            if attr == "inv_no" and getattr(obj, "name", ""):
                return f"{v} {obj.name}"
            if attr == "plate":
                from .orders import fmt_plate
                return " ".join(x for x in (getattr(obj, "make", ""), getattr(obj, "model", ""), fmt_plate(v)) if x)
            extra = ""
            woid = getattr(obj, "work_order_id", None)
            if woid and woid in orders:
                extra = f" · наряд {orders[woid]}"
            return f"{v}{extra}"
    woid = getattr(obj, "work_order_id", None)
    if woid and woid in orders:
        return f"наряд {orders[woid]}"
    return ""


@dataclass
class Item:
    row: AuditLog
    who: str
    who_id: object
    verb: str
    what: str
    target: str
    section: str
    changes: list = field(default_factory=list)   # [(поле, было, стало)] или [(поле, None, значение)]


def describe(db: Session, rows: list[AuditLog]) -> list[Item]:
    from ..models import Lift, Role, WorkOrder
    from ..models.orders import FUEL_LEVELS, INTAKE_LABELS, WO_STATUSES
    models = _models()
    users = {u.id: u for u in db.scalars(select(User))}
    names = {str(k): v.full_name for k, v in users.items()}
    names.update({str(r.id): r.name for r in db.scalars(select(Role))})
    names.update({str(l.id): f"пост {l.number}" for l in db.scalars(select(Lift))})
    from ..models import Permission
    perms = {p.code: p.name for p in db.scalars(select(Permission))}
    # Подписи объектов — пачкой по каждой таблице
    by_table: dict[str, set] = {}
    for r in rows:
        by_table.setdefault(r.table, set()).add(r.record_id)
    objs = {}
    orders = {}
    for table, ids in by_table.items():
        model = models.get(table)
        if not model or not hasattr(model, "id"):
            continue
        uids = []
        for i in ids:
            try:
                uids.append(uuid.UUID(i))
            except ValueError:
                pass
        for o in db.scalars(select(model).where(model.id.in_(uids))):
            objs[(table, str(o.id))] = o
    wo_ids = {getattr(o, "work_order_id", None) for o in objs.values()} - {None}
    wo_ids |= {o.id for (t, _), o in objs.items() if t == "work_orders"}
    orders = {o.id: o.number for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_(list(wo_ids))))} if wo_ids else {}
    for wid, num in orders.items():
        names[str(wid)] = num

    def fmt(k, v):
        if v is None or v == "":
            return "—"
        if k in ("password_hash", "pin_hash"):
            return "изменён"
        if isinstance(v, bool):
            return "да" if v else "нет"
        if k in ("status", "to_status", "from_status") and v in WO_STATUSES:
            return WO_STATUSES[v]
        if k == "permission" and v in perms:
            return perms[v]
        if k == "fuel_level" and v in FUEL_LEVELS:
            return FUEL_LEVELS[v]
        if k == "intake_items" and isinstance(v, list):
            return ", ".join(INTAKE_LABELS.get(x, x) for x in v) or "—"
        if isinstance(v, str) and len(v) == 36 and v.count("-") == 4:
            return names.get(v, "—")
        if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-" and v[7:8] == "-":
            try:
                if len(v) == 10:
                    return date.fromisoformat(v).strftime("%d.%m.%Y")
                from .attendance import local_tz
                return datetime.fromisoformat(v).astimezone(local_tz()).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                return v
        if isinstance(v, (dict, list)):
            return "…"
        s = str(v)
        return s if len(s) <= 160 else s[:157] + "…"

    out = []
    for r in rows:
        u = users.get(actor_id(r))
        obj =objs.get((r.table, r.record_id))
        target = _label_of(obj, orders) if obj is not None else ""
        if not target and obj is not None:  # право роли / сотрудника, карта, отметка табеля — чьё это
            owner = getattr(obj, "user_id", None) or getattr(obj, "role_id", None)
            target = names.get(str(owner), "") if owner else ""
        if not target and r.changes:
            owner = r.changes.get("user_id") or r.changes.get("role_id")
            target = names.get(str(owner), "") if owner else ""
        if r.table == "users" and r.action in ("login", "logout", "view_as", "view_as_stop"):
            target = ""
        ch = []
        if r.action == "update":
            for k, v in (r.changes or {}).items():
                if k in SKIP_SHOW:
                    continue
                old, new = (v + [None, None])[:2] if isinstance(v, list) else (None, v)
                ch.append((FIELD_NAMES.get(k, k), fmt(k, old), fmt(k, new)))
        elif r.action in ("create", "delete"):
            for k in CREATE_FIELDS:
                if (r.changes or {}).get(k) not in (None, "", [], {}):
                    ch.append((FIELD_NAMES.get(k, k), None, fmt(k, r.changes[k])))
        else:
            for k, v in (r.changes or {}).items():
                ch.append((FIELD_NAMES.get(k, k), None, fmt(k, v)))
        out.append(Item(row=r, who=u.full_name if u else "Система", who_id=u.id if u else None,
                        verb=ACTIONS.get(r.action, r.action), what=TABLE_NAMES.get(r.table, r.table), target=target,
                        section=SECTIONS.get(TABLE_SECTION.get(r.table, ""), ("",))[0], changes=ch))
    return out


def changes_text(it: Item) -> str:
    return "; ".join(f"{k}: {a} → {b}" if a is not None else f"{k}: {b}" for k, a, b in it.changes)
