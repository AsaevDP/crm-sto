"""Сотрудники, роли, права, карты, присутствие."""
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Entity, now, UTCDateTime


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
    # Приём и увольнение. Уволенный не удаляется: active=False, вся история остаётся (вкладка «Архив»).
    hired_on: Mapped[date | None] = mapped_column(Date, nullable=True)      # пусто — берём дату создания
    dismissed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    dismiss_reason: Mapped[str] = mapped_column(Text, default="")
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Запчастист работает в паре с менеджером (11.11.2026): при приёмке у этого менеджера подставляется сам
    works_with_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Куда платить зарплату (11.11.2026): наличными / на карту / на счёт; номер карты или IBAN, банк
    pay_method: Mapped[str | None] = mapped_column(String(10), default="cash", nullable=True)
    pay_account: Mapped[str | None] = mapped_column(String(64), default="", nullable=True)
    pay_bank: Mapped[str | None] = mapped_column(String(60), default="", nullable=True)

    role: Mapped[Role] = relationship(foreign_keys=[role_id], lazy="joined")

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def hired_date(self) -> date:
        return self.hired_on or (self.created_at.astimezone().date() if self.created_at else date.today())

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
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    method: Mapped[str] = mapped_column(String(10), default="card")  # card / manual
    comment: Mapped[str] = mapped_column(Text, default="")


class AttendanceAck(Entity):
    """24.11.2026 (брат): «Принял к сведению» по табелю. Опоздание / прогул / нет отметки ухода считаются из отметок
    на лету (services/lateness.py), здесь — только кто и что уже увидел.
    side: self — сам сотрудник (опоздание), boss — руководство (бухгалтер, директор, зам, админ: прогул, нет ухода,
    опоздание директора или зама — только админ). Достаточно одного руководителя — у остальных тоже гаснет."""
    __tablename__ = "attendance_acks"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)   # о ком
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(10))       # late / absent / no_out
    side: Mapped[str] = mapped_column(String(10))       # self / boss
    acked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)


# 24.11.2026 (брат): решение руководства по опозданию / неявке / раннему уходу / задержке после работы.
ATT_DECISIONS = {"leave": "Отпросился заранее", "excused": "Уважительная причина", "deduct": "Удержать за время",
                 "fine": "Удержать + штраф", "bonus": "Премия за задержку", "nobonus": "Без премии"}
ATT_REASONS = {"ill": "Заболел", "injury": "Травма", "car": "Сломалась машина не по его вине", "accident": "ДТП",
               "family": "Семейные обстоятельства", "other": "Другое"}


class AttendanceDecision(Entity):
    """Что решили по дню сотрудника (services/lateness.py). kind: late / absent / early / overtime.
    Удержание, штраф, премия — строки PayrollAccrual (их id здесь; поменяли решение — старые отменяются).
    note — что написал сам сотрудник («что делал после работы»)."""
    __tablename__ = "attendance_decisions"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(10))
    decision: Mapped[str] = mapped_column(String(10), default="")       # ATT_DECISIONS; "" — ещё не решили
    reason: Mapped[str] = mapped_column(String(10), default="")         # ATT_REASONS (уважительная)
    comment: Mapped[str] = mapped_column(Text, default="")              # комментарий руководителя
    note: Mapped[str] = mapped_column(Text, default="")                 # объяснение сотрудника
    hours: Mapped[int] = mapped_column(default=0)                      # сколько часов удержано
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deduct_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    fine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    bonus_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class AttendanceLeave(Entity):
    """«Отпросился заранее» (24.11.2026): директор / зам / админ отмечают в «Табеле» или на «Пульте» — в это время
    опоздание, неявка и ранний уход не считаются и ничего не удерживается."""
    __tablename__ = "attendance_leaves"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    t_from: Mapped[str] = mapped_column(String(5), default="08:00")
    t_to: Mapped[str] = mapped_column(String(5), default="19:00")
    comment: Mapped[str] = mapped_column(Text, default="")

