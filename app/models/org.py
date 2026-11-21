"""Организация: филиалы, настройки, подъёмники, счётчик номеров нарядов."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, Entity, now, UTCDateTime


class Branch(Base):
    """Филиал (СТО). У самого филиала нет branch_id — он и есть филиал."""
    __tablename__ = "branches"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now, onupdate=now)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120), default="")
    code: Mapped[str] = mapped_column(String(20))  # идёт в номер наряда: СТО1


class Setting(Entity):
    """Настройки «ключ → значение (JSON)» для каждого филиала."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("branch_id", "key"),)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[dict] = mapped_column(JSON, default=dict)


# 21.11.2026 (брат): «Рабочие места» — пять видов, у каждого свои номера (1…лимит) и своя должность мастера
PLACE_KINDS = {   # вид: (одно место, группа, должность мастера, сколько мест всего)
    "lift": ("Подъёмник", "Подъёмники", "mechanic", 20),
    "electric": ("Электрик", "Электрики", "electrician", 5),
    "motor": ("Моторист", "Мотористы", "motorist", 5),
    "wash": ("Мойка", "Мойка", "washer", 5),
    "detail": ("Детейлинг", "Детейлинг", "detailer", 5),
}


class Lift(Entity):
    """Рабочее место (было «подъёмник / пост»): вид + номер внутри вида, закреплённый мастер, работает или нет."""
    __tablename__ = "lifts"
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60), default="")   # необязательная подпись: «Подвеска», «Развал»
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Закреплённый мастер: всегда один и тот же человек на этом месте
    mechanic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", use_alter=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default="lift", server_default="")

    @property
    def label(self) -> str:
        """«Подъёмник 3», «Электрик 1», «Мойка 2»."""
        return f"{PLACE_KINDS.get(self.kind or 'lift', PLACE_KINDS['lift'])[0]} {self.number}"

    @property
    def full_label(self) -> str:
        return self.label + (f" · {self.name}" if self.name else "")


class OrderCounter(Entity):
    """Счётчик номеров нарядов по годам (сбрасывается каждый год)."""
    __tablename__ = "order_counters"
    __table_args__ = (UniqueConstraint("branch_id", "year"),)
    year: Mapped[int] = mapped_column(Integer)
    last: Mapped[int] = mapped_column(Integer, default=0)
