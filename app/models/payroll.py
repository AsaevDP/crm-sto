"""Зарплата: конструктор схем (см. docs/schema.md, раздел 2а)."""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity

MONEY = Numeric(12, 2)


class PayScheme(Entity):
    __tablename__ = "pay_schemes"
    name: Mapped[str] = mapped_column(String(120))
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class PaySchemeLine(Entity):
    __tablename__ = "pay_scheme_lines"
    scheme_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("pay_schemes.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))          # salary / percent / per_unit
    salary_period: Mapped[str | None] = mapped_column(String(10), nullable=True)  # day / month
    base: Mapped[str | None] = mapped_column(String(20), nullable=True)  # works / parts_sale / parts_margin
    # Фильтр (всё необязательно): группа, конкретная деталь, откуда деталь
    filter_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("part_groups.id"), nullable=True)
    filter_catalog_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("parts_catalog.id"), nullable=True)
    filter_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # purchased / from_stock
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4))  # % или сумма
    priority: Mapped[int] = mapped_column(Integer, default=0)


class UserPayScheme(Entity):
    __tablename__ = "user_pay_schemes"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    scheme_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("pay_schemes.id"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class PartGroup(Entity):
    __tablename__ = "part_groups"
    name: Mapped[str] = mapped_column(String(120))


class PartGroupItem(Entity):
    __tablename__ = "part_group_items"
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("part_groups.id", ondelete="CASCADE"))
    catalog_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("parts_catalog.id"))


class PayrollAccrual(Entity):
    __tablename__ = "payroll_accruals"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)
    wo_part_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_parts.id"), nullable=True)
    scheme_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("pay_scheme_lines.id"), nullable=True)
    base_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    on_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="accrued")  # accrued / paid / cancelled
    kind: Mapped[str] = mapped_column(String(20), default="percent")  # percent / per_unit / salary / deduction
    note: Mapped[str] = mapped_column(String(300), default="")


class PayrollPayout(Entity):
    __tablename__ = "payroll_payouts"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
