"""Оплаты и журнал действий."""
import uuid
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity


class Payment(Entity):
    __tablename__ = "payments"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    method: Mapped[str] = mapped_column(String(10))   # cash / card / terminal
    purpose: Mapped[str] = mapped_column(String(10))  # prepay / extra / full
    receipt_no: Mapped[str] = mapped_column(String(60), default="")
    comment: Mapped[str] = mapped_column(String(300), default="")

PAY_METHODS = {"cash": "Наличные", "card": "Безнал", "terminal": "Терминал"}
PAY_PURPOSES = {"prepay": "Предоплата", "extra": "Доплата", "full": "Полная оплата"}


class AuditLog(Entity):
    """Кто, когда, что изменил. Пишется автоматически (app/audit.py)."""
    __tablename__ = "audit_log"
    table: Mapped[str] = mapped_column(String(60), index=True)
    record_id: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(10))  # create / update / delete / login / logout
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    # 12.11.2026: сделал админ «глазами сотрудника» (created_by — сотрудник, тут — кто из админов на самом деле)
    via_admin_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
