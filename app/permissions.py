"""Права доступа, роли по умолчанию и меню.

Права роли + разрешения сотрудника − запреты сотрудника = итоговые права.
У админа все права всегда (чтобы нельзя было запереть самого себя).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RolePermission, User, UserPermission

# (код, название, группа)
PERMISSIONS = [
    ("dashboard.view", "Пульт руководителя", "Общее"),
    ("orders.view_all", "Видеть все наряды", "Наряды"),
    ("orders.view_own", "Видеть свои наряды", "Наряды"),
    ("orders.create", "Создавать наряды, приёмка, фото", "Наряды"),
    ("orders.diagnose", "Диагностика: находки и фото", "Наряды"),
    ("orders.approve", "Согласование с клиентом", "Наряды"),
    ("orders.cancel", "Отменять наряды", "Наряды"),
    ("orders.assign", "Назначать запчастиста, слесаря, подъёмник", "Наряды"),
    ("clients.view", "Клиенты и авто", "Наряды"),
    ("parts.pick", "Подбор запчастей в наряд", "Склад и закупки"),
    ("purchases.view", "Видеть закупки", "Склад и закупки"),
    ("purchases.manage", "Создавать закупки", "Склад и закупки"),
    ("stock.view", "Видеть склад и остатки", "Склад и закупки"),
    ("stock.manage", "Приход, ячейки, инвентаризация", "Склад и закупки"),
    ("stock.issue", "Выдача запчастей по запросу", "Склад и закупки"),
    ("stock.request_issue", "Просить выдачу запчастей со склада", "Склад и закупки"),
    ("razbor.view", "Видеть разбор (б/у)", "Разбор"),
    ("razbor.manage", "Заводить б/у детали", "Разбор"),
    ("razbor.export", "Экспорт для сайта", "Разбор"),
    ("razbor.sell", "Продажа б/у: резерв под покупателя, «продано»", "Разбор"),
    ("money.view", "Видеть деньги и оплаты", "Деньги"),
    ("money.accept", "Принимать оплату", "Деньги"),
    ("money.close_with_debt", "Закрывать наряд с долгом", "Деньги"),
    ("prices.view_purchase", "Видеть закупочные цены", "Деньги"),
    ("prices.view_sale", "Видеть цены продажи", "Деньги"),
    ("attendance.view_all", "Табель всех сотрудников", "Сотрудники"),
    ("attendance.view_own", "Свой табель", "Сотрудники"),
    ("attendance.edit", "Править отметки табеля", "Сотрудники"),
    ("payroll.view_own", "Своя зарплата", "Сотрудники"),
    ("payroll.view_all", "Зарплата всех", "Сотрудники"),
    ("payroll.manage", "Схемы зарплаты", "Сотрудники"),
    ("tools.view", "Инструмент всех сотрудников", "Инструмент"),
    ("tools.view_own", "Свой инструмент", "Инструмент"),
    ("tools.manage", "Выдача инструмента и проверки", "Инструмент"),
    ("settings.access", "Настройки", "Администрирование"),
    ("users.manage", "Сотрудники и доступ", "Администрирование"),
    ("audit.view", "Журнал действий", "Администрирование"),
]
ALL_CODES = [p[0] for p in PERMISSIONS]

# Роли по умолчанию: код → (название, цвет, права). Совпадает с docs/statuses.md, раздел 3.
_OWN = ["payroll.view_own", "attendance.view_own", "tools.view_own"]
DEFAULT_ROLES = {
    "manager": ("Менеджер", "blue", [
        "dashboard.view", "orders.view_all", "orders.create", "orders.diagnose", "orders.approve",
        "orders.cancel", "orders.assign", "clients.view", "purchases.view", "stock.request_issue", "stock.view", "money.view", "money.accept",
        "prices.view_sale", "attendance.view_all", "tools.view", "razbor.view", "razbor.sell", *_OWN]),
    "partsman": ("Запчастист", "indigo", [
        "orders.view_own", "parts.pick", "purchases.view", "purchases.manage", "stock.view",
        "razbor.view", "prices.view_purchase", "prices.view_sale", *_OWN]),
    "storekeeper": ("Кладовщик", "orange", [
        "orders.view_all", "purchases.view", "stock.view", "stock.manage", "stock.issue",
        "razbor.view", "tools.view", "tools.manage", *_OWN]),
    "content": ("Кладовщик-контентщик", "teal", [
        "stock.view", "stock.manage", "stock.issue", "razbor.view", "razbor.manage", "razbor.export", "razbor.sell",
        "prices.view_sale", *_OWN]),
    "mechanic": ("Слесарь", "green", ["orders.view_own", "orders.diagnose", "stock.request_issue", *_OWN]),
    "admin": ("Админ", "graphite", ALL_CODES),
}
ADMIN_ROLE = "admin"

# Системные цвета (как в app.css): код → название
SYSTEM_COLORS = {
    "blue": "Синий", "indigo": "Индиго", "purple": "Фиолетовый", "teal": "Бирюзовый",
    "green": "Зелёный", "yellow": "Жёлтый", "orange": "Оранжевый", "red": "Красный",
    "graphite": "Графит",
}

# Меню: (адрес, название, иконка, какое право нужно — достаточно любого из списка)
MENU = [
    ("/pult", "Пульт", "pult", ["dashboard.view"]),
    ("/orders", "Наряды", "orders", ["orders.view_all", "orders.view_own"]),
    ("/clients", "Клиенты", "users", ["clients.view"]),
    ("/stock", "Склад", "stock", ["stock.view"]),
    ("/purchases", "Закупки", "cart", ["purchases.view"]),
    ("/razbor", "Разбор", "car", ["razbor.view"]),
    ("/money", "Деньги", "money", ["money.view"]),
    ("/timesheet", "Табель", "clock", ["attendance.view_all", "attendance.view_own"]),
    ("/payroll", "Зарплата", "chart", ["payroll.view_own", "payroll.view_all"]),
    ("/tools", "Инструмент", "tool", ["tools.view", "tools.view_own"]),
    ("/settings", "Настройки", "gear", ["settings.access", "users.manage"]),
    ("/audit", "Журнал", "list", ["audit.view"]),
]


def effective_permissions(db: Session, user: User) -> set[str]:
    if user.role.code == ADMIN_ROLE:
        return set(ALL_CODES)
    perms = set(db.scalars(select(RolePermission.permission).where(RolePermission.role_id == user.role_id)))
    for up in db.scalars(select(UserPermission).where(UserPermission.user_id == user.id)):
        if up.allow:
            perms.add(up.permission)
        else:
            perms.discard(up.permission)
    return perms


def menu_for(perms: set[str]) -> list[dict]:
    return [
        {"href": href, "title": title, "icon": icon}
        for href, title, icon, need in MENU
        if any(p in perms for p in need)
    ]
