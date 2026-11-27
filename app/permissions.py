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
    # 27.11.2026 (брат): директору / заму админ может выключить кружки «для сведения» (дела сотрудников, сиреневая обводка)
    ("badges.hide_info", "Не показывать кружки «для сведения» (сиреневая обводка) — директор, зам", "Общее"),
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
    ("schedule.queue", "Менять очередь нарядов мастера (↑ ↓ в «Записи»)", "Наряды"),
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
    # 11.11.2026: слесарь смотрит склад (название, наличие, количество — без цен) и просит запчасть через менеджера
    ("stock.look", "Смотреть склад и разбор: название и количество, без цен", "Склад и закупки"),
    ("parts.ask", "Просить запчасть со склада под свой наряд (согласует менеджер)", "Склад и закупки"),
    ("stock.manage", "Приход, ячейки, инвентаризация", "Склад и закупки"),
    # 17.11.2026 (брат): приёмка — отдельное право (только с № накладной). У кладовщика есть; заму, директору, менеджеру —
    # резервом, включается в «Настройки → Приёмка без кладовщика» (по умолчанию выключено)
    ("stock.receive", "Принимать запчасти на склад (только с № накладной)", "Склад и закупки"),
    ("purchases.stock", "Заказывать на склад — свои закупки без наряда (кладовщик)", "Склад и закупки"),
    ("purchases.all", "Видеть закупки всех сотрудников (с фильтром по человеку)", "Склад и закупки"),
    ("stock.keep", "«Всегда в наличии»: вести список запчастей и норму", "Склад и закупки"),
    ("stock.issue", "Выдача запчастей по запросу", "Склад и закупки"),
    ("stock.request_issue", "Просить выдачу запчастей со склада", "Склад и закупки"),
    # 12.11.2026 (брат): мойка и детейлинг — химия со склада (расход СТО, в счёт клиенту не идёт)
    ("chem.ask", "Просить у кладовщика химию для мойки и ухода (мойщик, детейлер)", "Мойка"),
    ("wash.view", "Раздел «Мойка»: машины на мойке, химия, кто сколько взял", "Мойка"),
    ("wash.buy", "Заказывать химию для мойки у поставщика («Закупка для мойки»)", "Мойка"),
    # 18.11.2026 (брат): прайс мойки (услуги и цены) и боксы мойки правят менеджер, директор, зам, админ; мойщик — только видит
    ("wash.prices", "Прайс мойки: услуги, цены, боксы мойки", "Мойка"),
    # 18.11.2026 (брат): цену новой детали разбора и её изменение утверждают директор и зам (админ — всё)
    ("razbor.price", "Утверждать цены разбора (новые детали и изменение цены)", "Разбор"),
    ("razbor.view", "Видеть разбор (б/у)", "Разбор"),
    ("razbor.manage", "Заводить б/у детали", "Разбор"),
    ("razbor.export", "Экспорт для сайта", "Разбор"),
    # 22.11.2026 (брат): заявки покупателей с сайта — принять (детали в резерв) или отказать
    ("site.orders", "Заявки с сайта: принять / отказать", "Разбор"),
    ("razbor.sell", "Продажа б/у: резерв под покупателя, «продано»", "Разбор"),
    # 20.11.2026 (брат): склад — две зоны со своими окошками. Б/у под наряд выдаёт кладовщик разбора
    ("razbor.issue", "Выдача б/у деталей под наряд («Готово к выдаче», «Выдано»)", "Разбор"),
    ("razbor.stock", "Ячейки и инвентаризация деталей разбора", "Разбор"),
    ("money.view", "Видеть деньги и оплаты", "Деньги"),
    ("money.accept", "Принимать оплату и оформлять возврат клиенту", "Деньги"),
    # «Закрывать наряд с долгом» убрано 11.11.2026: в долг не работаем, машина выдаётся только после полной оплаты
    ("prices.view_purchase", "Видеть закупочные цены", "Деньги"),
    ("prices.view_sale", "Видеть цены продажи", "Деньги"),
    ("attendance.view_all", "Табель всех сотрудников", "Сотрудники"),
    ("attendance.view_own", "Свой табель", "Сотрудники"),
    ("attendance.edit", "Править отметки табеля", "Сотрудники"),
    # 24.11.2026 (брат): не пришёл / не отметил уход (и опоздание директора, зама — админу) — «Принял к сведению»
    ("attendance.alerts", "Табель: опоздания и пропущенные отметки сотрудников", "Сотрудники"),
    # 24.11.2026 (брат, вечер): решения по опозданию / неявке / раннему уходу / задержке, «Отпросился», «Настройки → Табель»
    ("attendance.decide", "Табель: решать (удержать, штраф, уважительная, отпросился, премия)", "Сотрудники"),
    ("payroll.view_own", "Своя зарплата («Моя зарплата»)", "Сотрудники"),
    # 20.11.2026 (брат): «Зарплата сотрудников» — бухгалтер всегда, директору — галочку ставит админ, заму — на срок по запросу
    ("payroll.view_all", "«Зарплата сотрудников» — видеть всех (директору даёт админ)", "Сотрудники"),
    ("payroll.approve", "Зарплата: утверждать к выплате (директор; зам — когда директор дал доступ)", "Сотрудники"),
    ("payroll.grant", "Давать заму доступ к «Зарплате сотрудников» на срок", "Сотрудники"),
    ("payroll.manage", "Схемы зарплаты и выплаты", "Сотрудники"),
    ("payroll.check", "Зарплата: проверять неделю (бухгалтер)", "Сотрудники"),
    ("payroll.pay", "Зарплата: отмечать выдачу денег за неделю", "Сотрудники"),
    # 12.11.2026 (брат): аванс конкретному сотруднику выдают только директор и зам — бухгалтер нет
    ("payroll.advance", "Зарплата: выдавать аванс сотруднику", "Сотрудники"),
    ("payroll.edit", "Зарплата: исправлять начисления, добавлять премии и штрафы (даёт админ)", "Сотрудники"),
    ("staff.dismiss", "Увольнять и возвращать сотрудников, архив уволенных", "Сотрудники"),
    ("tools.view", "Инструмент всех сотрудников", "Инструмент"),
    ("tools.view_own", "Свой инструмент", "Инструмент"),
    ("tools.manage", "Выдача инструмента и проверки", "Инструмент"),
    ("settings.access", "Настройки", "Администрирование"),
    ("users.manage", "Сотрудники и доступ", "Администрирование"),
    ("audit.view", "Журнал действий", "Администрирование"),
    # 25.11.2026 (брат): фотоархив и архив клиентов — внутреннее, только владелец (админ); удаляет фото только админ
    ("data.archive", "Фотоархив и архив клиентов (владелец)", "Администрирование"),
]
ALL_CODES = [p[0] for p in PERMISSIONS]

# Роли по умолчанию: код → (название, цвет, права). Совпадает с docs/statuses.md, раздел 3.
_OWN = ["payroll.view_own", "attendance.view_own", "tools.view_own"]
_MASTER = ["orders.view_own", "orders.diagnose", "stock.request_issue", "lifts.take", "schedule.view", "stock.look",
           "parts.ask", *_OWN]
_WASH = ["orders.view_own", "schedule.view", "chem.ask", *_OWN]

# Мастера — кто делает работы в наряде (12.11.2026): у каждой работы свой исполнитель. Код роли → название типа работ
MASTERS = {"mechanic": "Слесарь", "electrician": "Автоэлектрик", "motorist": "Моторист",
           "washer": "Мойщик", "detailer": "Детейлер"}
WASHERS = ("washer", "detailer")


def is_master(user) -> bool:
    return bool(user and user.role and user.role.code in MASTERS)
DEFAULT_ROLES = {
    "manager": ("Менеджер", "blue", [
        # 13.11.2026 (брат): менеджер запчасти не добавляет (только выбирает варианты и убирает в архив) — без parts.offer
        "dashboard.view", "orders.view_all", "orders.create", "orders.diagnose", "orders.approve",
        "orders.cancel", "orders.assign", "clients.view", "schedule.view", "schedule.manage", "schedule.queue", "purchases.view", "stock.request_issue", "stock.pickup", "stock.view", "money.view", "money.accept",
        "purchases.by_partsman",   # 19.11.2026 (брат): «Заказы запчастистов» — только посмотреть, когда приедут детали
        # 20.11.2026 (брат): менеджер химию не заказывает (wash.buy — у запчастиста), в «Мойке» — очередь и прайс
        "prices.view_sale", "attendance.view_all", "tools.view", "razbor.view", "razbor.sell", "wash.view", "wash.prices", *_OWN]),
    "partsman": ("Запчастист", "indigo", [
        "orders.view_own", "parts.pick", "purchases.view", "purchases.manage", "purchases.mine", "stock.view",
        "razbor.view", "prices.view_purchase", "prices.view_sale",
        "wash.view", "wash.buy",   # 20.11.2026 (брат): «Мойка» у запчастиста — только химия и закупки для мойки
        *_OWN]),
    "storekeeper": ("Кладовщик", "orange", [
        "orders.view_all", "purchases.view", "purchases.by_partsman", "stock.view", "stock.manage", "stock.issue",
        "stock.receive", "purchases.stock", "stock.keep", "razbor.view", "tools.view", "tools.manage", *_OWN]),
    # 20.11.2026 (брат): зона кладовщика разбора — только б/у (выдача под наряд, ячейки, инвентаризация);
    # новый склад (stock.manage / stock.issue) — зона обычного кладовщика
    "content": ("Кладовщик-контентщик", "teal", [
        "stock.view", "stock.receive", "razbor.view", "razbor.manage", "razbor.export", "razbor.sell",
        "razbor.issue", "razbor.stock", "prices.view_sale", "site.orders", *_OWN]),
    # 20.11.2026 (брат): главный кладовщик — обе зоны (склад новых + разбор) и инструмент. Работают втроём — контролирует
    # и подменяет (цифры по табелю: кладовщик зоны на работе — «для сведения»); один на всё — все обязанности на нём
    "headkeeper": ("Главный кладовщик", "orange", [
        "orders.view_all", "purchases.view", "purchases.by_partsman", "stock.view", "stock.manage", "stock.issue",
        "stock.receive", "purchases.stock", "stock.keep", "tools.view", "tools.manage",
        "razbor.view", "razbor.manage", "razbor.export", "razbor.sell", "razbor.issue", "razbor.stock", "prices.view_sale",
        "site.orders", *_OWN]),
    "mechanic": ("Слесарь", "green", _MASTER),
    # 12.11.2026 (брат): автоэлектрик и моторист — как слесарь (свои работы в наряде, запчасти, сообщения)
    "electrician": ("Автоэлектрик", "yellow", _MASTER),
    "motorist": ("Моторист", "red", _MASTER),
    # Мойщик и детейлер: свои работы в наряде, химия у кладовщика; без разбора и запчастей
    "washer": ("Мойщик", "teal", _WASH),
    "detailer": ("Детейлер", "indigo", _WASH),
    # Директор: видит всё и всю бухгалтерию, сам ничего не «крутит» в нарядах
    "director": ("Директор", "purple", [
        "dashboard.view", "reports.view", "reports.finance", "orders.view_all", "clients.view", "schedule.view",
        "schedule.manage",   # 22.11.2026 (брат): директор тоже записывает клиентов, как менеджер
        "schedule.queue",    # 22.11.2026 (брат): очередь нарядов мастера (↑ ↓) — менеджер, директор, зам, админ
        "purchases.view", "purchases.by_partsman", "stock.view", "razbor.view", "money.view", "prices.view_purchase", "prices.view_sale",
        # 20.11.2026 (брат): «Зарплата сотрудников» (payroll.view_all) директору — только галочкой админа; утверждать —
        # payroll.approve; заму доступ на срок — payroll.grant
        "attendance.view_all", "attendance.edit", "attendance.alerts", "attendance.decide",   # 24.11.2026
        "tools.view", "audit.view", "staff.dismiss", "orders.grant_access",
        "payroll.advance", "payroll.approve", "payroll.grant",
        "wash.view", "wash.buy", "wash.prices", "razbor.price", "purchases.all", "stock.keep", "site.orders", *_OWN]),
    # Заместитель: операционные отчёты и загрузка, без прибыли/себестоимости; может записывать и согласовывать
    "deputy": ("Заместитель директора", "yellow", [
        "dashboard.view", "reports.view", "orders.view_all", "orders.approve", "parts.offer", "orders.assign", "clients.view",
        "schedule.view", "schedule.manage", "schedule.queue", "purchases.view", "stock.view", "razbor.view", "money.view",
        "prices.view_sale", "attendance.view_all", "attendance.edit", "attendance.alerts", "attendance.decide", "tools.view", "orders.grant_access", "payroll.advance", "wash.view", "wash.buy", "wash.prices", "razbor.price",
        "payroll.approve",   # 20.11.2026: утверждать — когда директор дал доступ к «Зарплате сотрудников» (на срок)
        "purchases.all", "stock.keep", "site.orders", *_OWN]),
    # Бухгалтер (11.11.2026): в выходные проверяет неделю и выдаёт зарплату. Суммы править — только с разрешения админа
    "accountant": ("Бухгалтер", "teal", [
        "reports.view", "money.view", "attendance.view_all", "attendance.edit", "attendance.alerts", "payroll.view_all", "payroll.check", "payroll.pay",
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
    ("/stock", "Склад", "stock", ["stock.view", "stock.look"]),
    # 12.11.2026: отдельный пункт кладовщику — запросы на выдачу (запчасти по нарядам и инструмент), с цифрой «сколько ждут»
    ("/stock/issues", "Запросы на выдачу", "inbox", ["stock.issue", "tools.manage", "razbor.issue"]),   # 20.11: и разбору
    # 12.11.2026: менеджеру — запросы на выдачу по его нарядам (ждут кладовщика / готово — забрать / выдано)
    ("/ready", "Запросы на выдачу", "inbox", ["stock.pickup"]),
    # 12.11.2026 (брат): мастерам — что они просили у кладовщика (запчасти, инструмент, химия) и что с этим сейчас
    ("/my-requests", "Мои запросы на выдачу", "inbox", ["stock.request_issue", "chem.ask"]),
    ("/purchases", "Закупки", "cart", ["purchases.view"]),
    ("/my-orders", "Мои заказы", "list", ["purchases.mine"]),
    ("/purchases/by-partsman", "Заказы запчастистов", "users", ["purchases.by_partsman"]),
    # 12.11.2026: мойка — машины на мойке, химия, закупка химии (менеджер, директор, зам)
    ("/wash", "Мойка", "drop", ["wash.view"]),
    ("/wash/my", "Химия", "drop", ["chem.ask"]),
    ("/razbor", "Разбор", "car", ["razbor.view"]),
    # 12.11.2026: второй кладовщик (разбор) — подготовить, выдать, отправить; цифра — сколько подготовить
    ("/razbor/shipping", "Выдача и отправка", "box", ["razbor.manage"]),
    # 18.11.2026: директору и заму — утвердить цены разбора (новые детали 🔴, изменение цены 🟠)
    ("/razbor/prices", "Цены разбора", "money", ["razbor.price"]),
    # 22.11.2026 (брат): «На сайт» — новые запчасти на продажу (отказы клиентов, «На продажу» со склада) и б/у разбора
    ("/site", "На сайт", "globe", ["razbor.export", "razbor.price"]),
    # 22.11.2026 (брат): заявки покупателей с сайта — кладовщик разбора, главный кладовщик, директор, зам, админ
    ("/site/orders", "Заявки с сайта", "inbox", ["site.orders"]),
    ("/money", "Деньги", "money", ["money.view"]),
    ("/timesheet", "Табель", "clock", ["attendance.view_all", "attendance.view_own"]),
    # 20.11.2026 (брат): два пункта — «Моя зарплата» (у каждого, кто получает зарплату) и «Зарплата сотрудников»
    # (бухгалтер; директор — по галочке админа; зам — по доступу от директора на срок; без доступа — экран «как открыть»)
    ("/payroll/my", "Моя зарплата", "chart", ["payroll.view_own"]),
    ("/payroll/weeks", "Зарплата сотрудников", "users", ["payroll.view_all", "payroll.approve"]),
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


def menu_title(href: str, title: str, perms: set[str]) -> str:
    """19.11.2026 (брат): кто видит в «Закупках» только свои (кладовщик — на склад, менеджер — химию) — «Мои закупки»;
    директор, зам, админ (видят всех) — «Закупки»."""
    if href == "/purchases" and "purchases.all" not in perms:
        return "Мои закупки"
    return title


def menu_for(perms: set[str]) -> list[dict]:
    return [
        {"href": href, "title": menu_title(href, title, perms), "icon": icon}
        for href, title, icon, need in MENU
        if any(p in perms for p in need)
        # 19.11.2026 (брат): у запчастиста закупки — в «Моих заказах» (переключатель «По нарядам | По накладным»)
        and not (href == "/purchases" and "purchases.mine" in perms and "purchases.by_partsman" not in perms)
        # 20.11.2026 (брат): у кого своих закупок нет (менеджер — химию больше не заказывает) — пункта нет
        and not (href == "/purchases" and not perms & {"purchases.all", "purchases.stock", "purchases.mine", "wash.buy"})
        # «Мои заказы» — только у запчастиста; кто видит заказы всех (админ, директор, кладовщик) — смотрит там
        and not (href == "/my-orders" and "purchases.by_partsman" in perms)
        and not (href == "/ready" and "stock.issue" in perms)   # кладовщик и админ видят это в «Запросах на выдачу»
        and not (href == "/wash/my" and "wash.view" in perms)   # «Химия» — мойщику; руководителю всё видно в «Мойке»
        # «Мои запросы на выдачу» — мастерам; у менеджера — «Запросы на выдачу», у кладовщика и админа — свои
        and not (href == "/my-requests" and ("stock.pickup" in perms or "stock.issue" in perms or "wash.view" in perms))
    ]
