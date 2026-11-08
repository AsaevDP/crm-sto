"""Сотрудники, роли, права, карты, присутствие."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Entity, now


class Role(Entity):
    __tablename__ = "roles"
    code: Mapped[str] = mapped_column(String(40))          # manager, mechanic…
    name: Mapped[str] = mapped_column(String(80))
    # Цвет интерфейса: системный цвет ("blue", "green"…) или свой "#rrggbb"
    color: Mapped[str] = mapped_column(String(20), default="blue")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(default=0)


class Permission(Base):
    """Справочник прав. Заполняется программой из app/permissions.py, общий для всех филиалов."""
    __tablename__ = "permissions"
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    group: Mapped[str] = mapped_column(String(60))


class RolePermission(Entity):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission"),)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("roles.id", ondelete="CASCADE"))
    permission: Mapped[str] = mapped_column(String(60), ForeignKey("permissions.code"))


class User(Entity):
    __tablename__ = "users"
    full_name: Mapped[str] = mapped_column(String(120))
    login: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200), default="")
    pin_hash: Mapped[str] = mapped_column(String(200), default="")
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("roles.id"))
    phone: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    role: Mapped[Role] = relationship(foreign_keys=[role_id], lazy="joined")

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def short_name(self) -> str:
        parts = self.full_name.split()
        return parts[1] if len(parts) > 1 else self.full_name


class UserPermission(Entity):
    """Точечное право сотрудника: allow=True — добавить, False — забрать."""
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission"),)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    permission: Mapped[str] = mapped_column(String(60), ForeignKey("permissions.code"))
    allow: Mapped[bool] = mapped_column(Boolean)


class StaffCard(Entity):
    """Карта/брелок как от домофона. uid — то, что «набирает» считыватель."""
    __tablename__ = "staff_cards"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    uid: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AccessPoint(Entity):
    __tablename__ = "access_points"
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20), default="gate")  # gate — проходная, login — вход в CRM
    counts_presence: Mapped[bool] = mapped_column(Boolean, default=True)


class AttendanceEvent(Entity):
    __tablename__ = "attendance_events"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    card_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("staff_cards.id"), nullable=True)
    point_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("access_points.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(10))  # in / out
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    method: Mapped[str] = mapped_column(String(10), default="card")  # card / manual
    comment: Mapped[str] = mapped_column(Text, default="")
