"""Клиенты, авто, наряды."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, now

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
# Статус позиции запчасти
PART_STATUSES = {
    "need": "нужна", "ordered": "заказана", "arrived": "пришла", "reserved": "в резерве",
    "issued": "выдана", "installed": "установлена", "returned": "возврат",
}


class Client(Entity):
    __tablename__ = "clients"
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")


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
    approval: Mapped[str] = mapped_column(String(10), default="pending")  # pending / yes / no
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WoPhoto(Entity):
    __tablename__ = "wo_photos"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    file: Mapped[str] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(String(10), default="intake")  # intake / diag / work


class WoWork(Entity):
    __tablename__ = "wo_works"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_findings.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)  # нормо-часы или пусто (фикс)
    qty: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=1)
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="todo")  # todo / doing / done


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


class WoWorkSession(Entity):
    """Отрезки времени слесаря: работа или пауза. Для пульта и реального времени работ."""
    __tablename__ = "wo_work_sessions"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id", ondelete="CASCADE"))
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)
    mechanic_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    lift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("lifts.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="work")  # work / pause
    pause_reason: Mapped[str] = mapped_column(String(200), default="")
