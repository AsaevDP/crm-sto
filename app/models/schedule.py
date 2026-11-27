"""Запись клиентов на будущее: к мастеру, на время (пункт 2)."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, UTCDateTime

APPT_STATUS = {"booked": "Записан", "arrived": "Приехал", "noshow": "Не приехал", "cancel": "Отменён"}


class Appointment(Entity):
    __tablename__ = "appointments"
    start_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True, index=True)
    lift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("lifts.id"), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("clients.id"), nullable=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("vehicles.id"), nullable=True)
    client_name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    car: Mapped[str] = mapped_column(String(120), default="")       # «Toyota Camry»
    plate: Mapped[str] = mapped_column(String(20), default="")
    work: Mapped[str] = mapped_column(Text, default="")             # что делать / жалоба
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="booked", index=True)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    # 20.11.2026 (брат): «Напомнить о записи» — менеджер позвонил, клиент подтвердил, что приедет
    called_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    called_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # 21.11.2026 (брат): мастер нажал «✓ Видел» — запоминаем, ЧТО он видел (мастер | время | статус, schedule.seen_key).
    # Перенесли время, сменили мастера или клиент «не приехал» — ключ другой, плитка у мастера снова горит.
    seen_key: Mapped[str] = mapped_column(String(80), default="")
    seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class MasterQueue(Entity):
    """Очередь нарядов мастера (22.11.2026, брат): менеджер / директор / зам / админ стрелками ↑ ↓ ставят наряд раньше
    или позже. rank — место в очереди этого мастера (меньше — раньше); наряда нет в таблице — порядок по умолчанию
    (в работе → на складе → ждём запчасти → по приёмке). Один наряд у разных мастеров — своё место у каждого."""
    __tablename__ = "master_queue"
    mechanic_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)


class MasterQueueRequest(Entity):
    """Мастер в режиме «Обязательная очередь» просит взять машину раньше (27.11.2026, брат) — как мойщик: причина →
    менеджер / директор / зам / админ «Согласен» (машина встаёт первой у этого мастера) или «Отказать» с причиной.
    status: new / ok / declined / cancelled."""
    __tablename__ = "master_queue_requests"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    mechanic_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    answer: Mapped[str] = mapped_column(Text, default="")
