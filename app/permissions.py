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
    ("reports.view", "Кабинет: отчёты, графики, загрузка мастеров", "Общее"),
    ("reports.finance", "Кабинет: прибыль, себестоимость, бухгалтерия", "Общее"),
    ("orders.view_all", "Видеть все наряды", "Наряды"),
    ("orders.view_own", "Видеть свои наряды", "Наряды"),
    ("orders.create", "Создавать наряды, приёмка, фото", "Наряды"),
    ("orders.diagnose", "Диагностика: находки и фото", "Наряды"),
    ("orders.approve", "Согласование с клиентом", "Наряды"),
    ("orders.cancel", "Отменять наряды", "Наряды"),
    ("parts.offer", "Добавлять в наряд запчасти и варианты на выбор клиенту (без закупки)", "Наряды"),
    ("orders.assign", "Назначать запчастиста, слесаря, подъёмник", "Наряды"),
    ("clients.view", "Клиенты и авто", "Наряды"),
    ("schedule.view", "Запись клиентов: видеть (слесарь — только свою)", "Наряды"),
    ("schedule.manage", "Записывать клиентов на время", "Наряды"),
    ("lifts.take", "Самому закрепляться за подъёмником", "Наряды"),
    ("parts.pick", "Подбор запчастей в наряд", "Склад и закупки"),
    ("purchases.view", "Видеть закупки", "Склад и закупки"),
    ("purchases.manage", "Создавать закупки", "Склад и закупки"),
    ("orders.grant_access", "Разрешать менеджеру видеть все наряды (на срок), открывать закрытые наряды заново "
                            "и переставлять пары менеджер ↔ запчастист", "Наряды"),
    ("stock.pickup", "«Готово к выдаче» — забирать подготовленные кладовщиком запчасти и закрывать выдачу", "Склад и закупки"),
    ("purchases.mine", "«Мои заказы» — свои закупки по своим нарядам (запчастист)", "Склад и закупки"),
    ("purchases.by_partsman", "«Заказы запчастистов» — заказы каждого запчастиста", "Склад и закупки"),
    ("stock.view", "Видеть склад и остатки", "Склад и закупки"),
    ("stock.manage", "Приход, ячейки, инвентаризация", "Склад и закупки"),
    ("stock.issue", "Выдача запчастей по запросу", "Склад и закупки"),
    ("stock.request_issue", "Просить выдачу запчастей со склада", "Склад и закупки"),
    ("razbor.view", "Видеть разбор (б/у)", "Разбор"),
    ("razbor.manage", "Заводить б/у детали", "Разбор"),
    ("razbor.export", "Экспорт для сайта", "Разбор"),
    ("razbor.sell", "Продажа б/у: резерв под покупателя, «продано»", "Разбор"),
    ("money.view", "Видеть деньги и оплаты", "Деньги"),
    ("money.accept", "Принимать оплату и оформлять возврат клиенту", "Деньги"),
    # «Закрывать наряд с долгом» убрано 11.11.2026: в долг не работаем, машина выдаётся только после полной оплаты
    ("prices.view_purchase", "Видеть закупочные цены", "Деньги"),
    ("prices.view_sale", "Видеть цены продажи", "Деньги"),
    ("attendance.view_all", "Табель всех сотрудников", "Сотрудники"),
    ("attendance.view_own", "Свой табель", "Сотрудники"),
    ("attendance.edit", "Править отметки табеля", "Сотрудники"),
    ("payroll.view_own", "Своя зарплата", "Сотрудники"),
    ("payroll.view_all", "Зарплата всех", "Сотрудники"),
    ("payroll.manage", "Схемы зарплаты и выплаты", "Сотрудники"),
    ("payroll.check", "Зарплата: проверять неделю (бухгалтер)", "Сотрудники"),
    ("payroll.pay", "Зарплата: отмечать выдачу денег за неделю", "Сотрудники"),
    ("payroll.edit", "Зарплата: исправлять начисления, добавлять премии и штрафы (даёт админ)", "Сотрудники"),
    ("staff.dismiss", "Увольнять и возвращать сотрудников, архив уволенных", "Сотрудники"),
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
        "dashboard.view", "orders.view_all", "orders.create", "orders.diagnose", "orders.approve", "parts.offer",
        "orders.cancel", "orders.assign", "clients.view", "schedule.view", "schedule.manage", "purchases.view", "stock.request_issue", "stock.pickup", "stock.view", "money.view", "money.accept",
        "prices.view_sale", "attendance.view_all", "tools.view", "razbor.view", "razbor.sell", *_OWN]),
    "partsman": ("Запчастист", "indigo", [
        "orders.view_own", "parts.pick", "purchases.view", "purchases.manage", "purchases.mine", "stock.view",
        "razbor.view", "prices.view_purchase", "prices.view_sale", *_OWN]),
    "storekeeper": ("Кладовщик", "orange", [
        "orders.view_all", "purchases.view", "purchases.by_partsman", "stock.view", "stock.manage", "stock.issue",
        "razbor.view", "tools.view", "tools.manage", *_OWN]),
    "content": ("Кладовщик-контентщик", "teal", [
        "stock.view", "stock.manage", "stock.issue", "razbor.view", "razbor.manage", "razbor.export", "razbor.sell",
        "prices.view_sale", *_OWN]),
    "mechanic": ("Слесарь", "green", ["orders.view_own", "orders.diagnose", "stock.request_issue", "lifts.take", "schedule.view", *_OWN]),
    # Директор: видит всё и всю бухгалтерию, сам ничего не «крутит» в нарядах
    "director": ("Директор", "purple", [
        "dashboard.view", "reports.view", "reports.finance", "orders.view_all", "clients.view", "schedule.view",
        "purchases.view", "purchases.by_partsman", "stock.view", "razbor.view", "money.view", "prices.view_purchase", "prices.view_sale",
        "attendance.view_all", "payroll.view_all", "tools.view", "audit.view", "staff.dismiss", "orders.grant_access", *_OWN]),
    # Заместитель: операционные отчёты и загрузка, без прибыли/себестоимости; может записывать и согласовывать
    "deputy": ("Заместитель директора", "yellow", [
        "dashboard.view", "reports.view", "orders.view_all", "orders.approve", "parts.offer", "orders.assign", "clients.view",
        "schedule.view", "schedule.manage", "purchases.view", "stock.view", "razbor.view", "money.view",
        "prices.view_sale", "attendance.view_all", "tools.view", "orders.grant_access", *_OWN]),
    # Бухгалтер (11.11.2026): в выходные проверяет неделю и выдаёт зарплату. Суммы править — только с разрешения админа
    "accountant": ("Бухгалтер", "teal", [
        "reports.view", "money.view", "attendance.view_all", "payroll.view_all", "payroll.check", "payroll.pay",
        "orders.view_all", *_OWN]),
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
    ("/reports", "Кабинет", "briefcase", ["reports.view", "reports.finance"]),
    ("/orders", "Наряды", "orders", ["orders.view_all", "orders.view_own"]),
    ("/schedule", "Запись", "calendar", ["schedule.view", "schedule.manage"]),
    ("/clients", "Клиенты", "users", ["clients.view"]),
    ("/stock", "Склад", "stock", ["stock.view"]),
    # 12.11.2026: отдельный пункт кладовщику — запросы на выдачу (запчасти по нарядам и инструмент), с цифрой «сколько ждут»
    ("/stock/issues", "Запросы на выдачу", "inbox", ["stock.issue", "tools.manage"]),
    # 12.11.2026: менеджеру — что кладовщик подготовил к выдаче (у кладовщика это в «Запросах на выдачу»)
    ("/ready", "Готово к выдаче", "check", ["stock.pickup"]),
    ("/purchases", "Закупки", "cart", ["purchases.view"]),
    ("/my-orders", "Мои заказы", "list", ["purchases.mine"]),
    ("/purchases/by-partsman", "Заказы запчастистов", "users", ["purchases.by_partsman"]),
    ("/razbor", "Разбор", "car", ["razbor.view"]),
    # 12.11.2026: второй кладовщик (разбор) — подготовить, выдать, отправить; цифра — сколько подготовить
    ("/razbor/shipping", "Выдача и отправка", "box", ["razbor.manage"]),
    ("/money", "Деньги", "money", ["money.view"]),
    ("/timesheet", "Табель", "clock", ["attendance.view_all", "attendance.view_own"]),
    ("/payroll", "Зарплата", "chart", ["payroll.view_own", "payroll.view_all"]),
    ("/tools", "Инструмент", "tool", ["tools.view", "tools.view_own"]),
    ("/settings", "Настройки", "gear", ["settings.access", "users.manage", "staff.dismiss", "orders.grant_access"]),
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
        # «Мои заказы» — только у запчастиста; кто видит заказы всех (админ, директор, кладовщик) — смотрит там
        and not (href == "/my-orders" and "purchases.by_partsman" in perms)
        and not (href == "/ready" and "stock.issue" in perms)   # кладовщик и админ видят это в «Запросах на выдачу»
    ]
