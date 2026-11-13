"""Клиенты, авто, наряды."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, now, UTCDateTime

MONEY = Numeric(12, 2)

# Статусы наряда (docs/statuses.md)
WO_STATUSES = {
    "created": "Создан",
    "diag": "Диагностика",
    "approve": "Согласование",
    "picking": "Подбор запчастей",
    "waiting": "Ждёт запчасти",
    "instock": "Запчасти на складе",
    "work": "В работе",
    "ready": "Готово",
    "paid": "Оплачено",
    "closed": "Выдан клиенту",
    "cancel": "Отменён",
}
# Короткие названия статусов для плашек в списках и на пульте (чтобы не обрезались)
WO_STATUS_SHORT = {
    "created": "Создан", "diag": "Диагностика", "approve": "Согласование", "picking": "Подбор",
    "waiting": "Ждёт запчасти", "instock": "Запчасти есть", "work": "В работе", "ready": "Готово",
    "paid": "Оплачено", "closed": "Выдан", "cancel": "Отменён",
}
# Активные наряды (машина на территории СТО)
WO_ACTIVE = ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready", "paid")
# Что есть в машине при приёмке (акт). Остальное — заметкой (решение владельца 09.11.2026).
INTAKE_ITEMS = {"docs": "Документы", "valuables": "Ценные вещи в салоне"}
# Названия для старых нарядов, где ещё отмечали комплектацию — только для показа
INTAKE_LABELS = {"spare": "Запаска", "kit": "Аптечка", "sign": "Знак аварийной остановки", "keys2": "Второй ключ",
                 "jack": "Домкрат", **INTAKE_ITEMS}
FUEL_LEVELS = {0: "Пусто", 25: "¼", 50: "½", 75: "¾", 100: "Полный"}

# Статус позиции запчасти
PART_STATUSES = {
    "need": "нужна", "ordered": "заказана", "arrived": "пришла", "reserved": "в резерве",
    "issued": "выдана", "installed": "установлена", "returned": "возврат",
    "option": "вариант", "declined": "не выбран",
}
# Позиции, которые НЕ идут в счёт, подбор и отчёты: возврат, вариант на выбор (клиент ещё не решил), невыбранный вариант
PART_OFF = ("returned", "option", "declined", "archived")   # «archived» — менеджер убрал в архив (13.11.2026)


class Client(Entity):
    __tablename__ = "clients"
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # «Постоянный» вручную (сам по себе клиент становится постоянным с 3 визитов — см. services/clients.py)
    is_regular: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)


class Vehicle(Entity):
    __tablename__ = "vehicles"
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clients.id"))
    make: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(60), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vin: Mapped[str] = mapped_column(String(32), default="", index=True)
    plate: Mapped[str] = mapped_column(String(20), default="", index=True)
    mileage: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkOrder(Entity):
    __tablename__ = "work_orders"
    number: Mapped[str] = mapped_column(String(40), unique=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clients.id"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicles.id"))
    status: Mapped[str] = mapped_column(String(20), default="created", index=True)
    scenario: Mapped[str] = mapped_column(String(1), default="B")  # A — без диагностики, B — с диагностикой
    manager_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    partsman_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    lift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("lifts.id"), nullable=True)
    mileage_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    complaint: Mapped[str] = mapped_column(Text, default="")
    payment_type: Mapped[str] = mapped_column(String(10), default="post")  # post — по факту, prepay — предоплата
    prepay_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    prepay_for: Mapped[str] = mapped_column(Text, default="")
    awaiting_prepay: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    closed_with_debt_comment: Mapped[str] = mapped_column(Text, default="")
    # Деньги
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)  # скидка клиенту на весь наряд, %
    # 11.11.2026 (брат): отдельно скидка только на работы, %. Сначала скидка на работы, потом — на весь наряд.
    works_discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=0, nullable=True)
    # 12.11.2026: наряд передали другому менеджеру — пока он не нажал «Принял», наряд в плитке «Мне передали»
    handover_from: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    handover_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    handover_pending: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True, index=True)
    # 13.11.2026 (брат): запчастист подбирает одновременно с согласованием — «Подбор завершён» отмечается тут,
    # менеджер после этого согласует с клиентом всё разом
    picked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    picked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Метка формы приёмки: повторная отправка той же формы не создаёт второй наряд
    intake_token: Mapped[str | None] = mapped_column(String(40), default=None, nullable=True, index=True)
    payroll_ignore_discount: Mapped[bool] = mapped_column(Boolean, default=False)  # зарплата без учёта скидки (главный админ)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Акт выдачи: ok — исправен, partial — не полностью исправен (handover_defects обязательно), advice — есть рекомендации
    handover_state: Mapped[str] = mapped_column(String(10), default="")
    handover_defects: Mapped[str] = mapped_column(Text, default="")
    handover_advice: Mapped[str] = mapped_column(Text, default="")
    # Акт приёма
    fuel_level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0, 25, 50, 75, 100 (%)
    intake_items: Mapped[list] = mapped_column(JSON, default=list)          # коды из INTAKE_ITEMS
    intake_note: Mapped[str] = mapped_column(Text, default="")              # замечания: повреждения и т.п.
    # Ремонт по гарантии (12.11.2026): open — открыт по гарантии, done — «Выполнены гарантийные работы» (закрыт второй раз),
    # rejected — «Не гарантийный случай» (закрыт обратно, ремонт — новым нарядом)
    warranty: Mapped[str] = mapped_column(String(10), default="", server_default="")
    warranty_reason: Mapped[str] = mapped_column(Text, default="", server_default="")     # с чем вернулся клиент
    warranty_result: Mapped[str] = mapped_column(Text, default="", server_default="")     # что произошло и почему
    warranty_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    warranty_closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    warranty_closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    warranty_prev_closed: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)   # первая выдача (для отчётов)


class WoStatusHistory(Entity):
    __tablename__ = "wo_status_history"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    from_status: Mapped[str] = mapped_column(String(20), default="")
    to_status: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text, default="")


class WoFinding(Entity):
    __tablename__ = "wo_findings"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(10), default="soon")  # urgent / soon / advice
    approval: Mapped[str] = mapped_column(String(10), default="pending")  # pending / yes / no
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class WoPhoto(Entity):
    __tablename__ = "wo_photos"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    file: Mapped[str] = mapped_column(String(255))          # путь внутри data/photos
    thumb: Mapped[str] = mapped_column(String(255), default="")  # миниатюра ~400px
    stage: Mapped[str] = mapped_column(String(10), default="intake")  # intake / diag / work


class WoWork(Entity):
    __tablename__ = "wo_works"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    # Цена работы — один из двух способов (выбирает менеджер):
    #   hours — нормо-часы × цена часа (цена часа своя в каждой работе), fixed — фиксированная цена
    pricing: Mapped[str] = mapped_column(String(10), default="hours")
    hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)  # нормо-часы
    rate: Mapped[Decimal] = mapped_column(MONEY, default=0)                    # цена нормо-часа, ₴
    qty: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=1)
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)  # итог по работе (hours×rate×qty или фикс×qty)
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="todo")  # todo / doing / paused / done
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    is_warranty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")   # гарантийная работа (0 ₴)
    done_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)  # кто нажал «Готово»


class WoPart(Entity):
    __tablename__ = "wo_parts"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    catalog_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("parts_catalog.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")  # пока номенклатура не подобрана
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=1)
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    status: Mapped[str] = mapped_column(String(10), default="need")
    picked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="")  # purchased / from_stock — для зарплаты
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)  # к какой работе
    # Деньги (внутреннее: закупка и наценка видят только запчастист и админ). price — цена продажи за единицу.
    cost: Mapped[Decimal] = mapped_column(MONEY, default=0)
    markup_pct: Mapped[int] = mapped_column(Integer, default=25)
    # Варианты на выбор клиенту (11.11.2026): одна группа = одна деталь разных фирм. Клиент выбирает один —
    # он идёт в подбор, остальные получают статус «не выбран».
    option_group: Mapped[str | None] = mapped_column(String(36), default="", nullable=True, index=True)
    # «Срочно нужна» (11.11.2026): ставит слесарь; видят менеджер, запчастист, кладовщик — по всей цепочке
    urgent: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)
    # 13.11.2026 (брат): до согласования с клиентом ничего не заказываем — запчастист только помечает «заказать»
    # (plan="order", plan_qty — купить всего), заказ поставщику появляется после «Согласовал с клиентом».
    plan: Mapped[str | None] = mapped_column(String(10), default="", nullable=True)
    plan_qty: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Архив (менеджер убрал позицию; можно вернуть, пока наряд не оплачен и не закрыт)
    prev_status: Mapped[str | None] = mapped_column(String(10), default="", nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    archived_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class Notification(Entity):
    """Сообщение сотруднику (11.11.2026): «Все запчасти на складе», «Срочная деталь пришла», «Слесарь просит срочно».
    Показывается окошком в углу, пока не закроют; список — по колокольчику. Клиенту ничего не отправляется."""
    __tablename__ = "notifications"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(String(400), default="")
    url: Mapped[str] = mapped_column(String(200), default="")
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)


# 12.11.2026: менеджер видит только свои наряды; ко всем — по запросу, админ / директор / зам разрешают на срок
ACCESS_STATUS = {"pending": "Ждёт решения", "approved": "Разрешено", "declined": "Отказано", "closed": "Закрыто досрочно"}
ACCESS_HOURS = {1: "1 час", 2: "2 часа", 3: "3 часа", 24: "день", 168: "неделю"}


class OrderAccess(Entity):
    """Запрос менеджера «показать все наряды»: причина → решение (срок) → действует до `until`."""
    __tablename__ = "order_access"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 12.11.2026: директора и зама не было на месте (по табелю) — решил сам тот, кто просил (на 1 час)
    self_decided: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


CLIENT_ACCESS_HOURS = {1: "1 час", 3: "3 часа", 24: "1 день", 48: "2 дня"}


class ClientAccess(Entity):
    """Доступ менеджера к карточке «чужого» клиента на срок (12.11.2026).
    Менеджер видит только своих клиентов (сегодняшних и 3 дня после выдачи машины). Нужен другой — пишет запрос
    (status=pending, client_id пусто, в reason — кого ищет и зачем). Директор / зам / админ находят клиента и дают доступ
    (approved, until) — можно и без запроса, из карточки клиента."""
    __tablename__ = "client_access"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("clients.id"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)   # pending / approved / declined / closed
    hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


MSG_STATUS ={"pending": "Ждёт согласования", "approved": "Согласовано", "declined": "Отказано"}


class OrderMessage(Entity):
    """Сообщение по наряду (11.11.2026): общение внутри команды наряда — слесарь ↔ менеджер ↔ запчастист.
    kind="text" — просто текст («нужно заказать хомут», «клиент просил ещё…»).
    kind="part" — слесарь выбрал запчасть на складе (есть в наличии) и просит её под наряд: менеджер согласует
    (тогда она в наряде, в резерве, и уходит кладовщику на выдачу) или отказывает. Кладовщик видит запрос заранее,
    но выдать может только после согласования. Инструмент сюда не идёт — его слесарь просит у кладовщика напрямую."""
    __tablename__ = "order_messages"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="text")
    to_ids: Mapped[list] = mapped_column(JSON, default=list)        # кому (id сотрудников)
    text: Mapped[str] = mapped_column(Text, default="")
    # Запрос запчасти
    stock_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stock_items.id"), nullable=True)
    catalog_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("parts_catalog.id"), nullable=True)
    qty: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="", index=True)   # "" (текст) / pending / approved / declined
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    answer: Mapped[str] = mapped_column(String(300), default="")    # причина отказа / комментарий менеджера
    wo_part_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


REOPEN_STATUS ={"pending": "Ждёт решения", "approved": "Открыт заново", "declined": "Отказано"}


class OrderReopen(Entity):
    """Запрос «открыть закрытый / отменённый наряд заново» (11.11.2026): просит любой, кто видит наряд,
    решают админ / директор / зам (право orders.grant_access) — как с доступом ко всем нарядам."""
    __tablename__ = "order_reopen"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    from_status: Mapped[str] = mapped_column(String(20), default="")   # closed / cancel — каким был наряд
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 12.11.2026: fix — открыть исправить ошибку; warranty — ремонт по гарантии (решает только директор / зам)
    kind: Mapped[str] = mapped_column(String(10), default="fix", server_default="fix")
    self_decided: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class ClientEvent(Entity):
    """Хронология карты клиента: всё, что происходило с клиентом и его авто.
    kind: order — новый наряд, status — смена статуса, finding — найдена проблема,
    approval — решение клиента, recommendation — рекомендация на будущее, act — акт, note — заметка."""
    __tablename__ = "client_events"
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("vehicles.id"), nullable=True)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    urgency: Mapped[str] = mapped_column(String(10), default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)  # для рекомендаций: уже сделали


URGENCY = {"urgent": "Срочно", "soon": "Скоро", "advice": "Рекомендация"}


class WoWorkSession(Entity):
    """Отрезки времени слесаря: работа или пауза. Для пульта и реального времени работ."""
    __tablename__ = "wo_work_sessions"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)
    mechanic_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    lift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("lifts.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="work")  # work / pause
    pause_reason: Mapped[str] = mapped_column(String(200), default="")
