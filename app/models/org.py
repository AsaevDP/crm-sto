"""Организация: филиалы, настройки, подъёмники, счётчик номеров нарядов."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, Entity, now


class Branch(Base):
    """Филиал (СТО). У самого филиала нет branch_id — он и есть филиал."""
    __tablename__ = "branches"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120), default="")
    code: Mapped[str] = mapped_column(String(20))  # идёт в номер наряда: СТО1


class Setting(Entity):
    """Настройки «ключ → значение (JSON)» для каждого филиала."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("branch_id", "key"),)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Lift(Entity):
    __tablename__ = "lifts"
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Закреплённый слесарь: всегда один и тот же человек на этом посту
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", use_alter=True), nullable=True)


class OrderCounter(Entity):
    """Счётчик номеров нарядов по годам (сбрасывается каждый год)."""
    __tablename__ = "order_counters"
    __table_args__ = (UniqueConstraint("branch_id", "year"),)
    year: Mapped[int] = mapped_column(Integer)
    last: Mapped[int] = mapped_column(Integer, default=0)
