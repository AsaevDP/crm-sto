"""Запись клиентов на будущее: к мастеру, на время (пункт 2)."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity

APPT_STATUS = {"booked": "Записан", "arrived": "Приехал", "noshow": "Не приехал", "cancel": "Отменён"}


class Appointment(Entity):
    __tablename__ = "appointments"
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
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
