"""Учёт инструмента сотрудников (шаг 6а, BRIEF 8а)."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, UTCDateTime

MONEY = Numeric(12, 2)

TOOL_STATUS = {"ok": "Исправен", "worn": "Износ", "broken": "Сломан", "lost": "Утерян", "written_off": "Списан"}
CHECK_KINDS = {"hire": "При приёме на работу", "dismissal": "При увольнении", "planned": "Плановая"}
CHECK_RESULTS = {"ok": "Исправен", "worn": "Естественный износ", "fault": "Сломан по вине", "lost": "Утерян"}
CHECK_STATUS = {"draft": "Идёт проверка", "done": "Ждёт решения админа", "approved": "Закрыта"}
# Категории (решение владельца 09.11.2026). «Личный» выдаётся слесарю при приёме на работу и числится за ним;
# остальной — общий: выдаётся временно под конкретный наряд и работу, потом возвращается на склад инструмента.
TOOL_CATEGORIES = {
    "personal": "Личный", "to": "Для ТО", "engine": "Двигатель", "chassis": "Ходовая", "brakes": "Тормоза",
    "electric": "Электрика и диагностика", "tires": "Шиномонтаж и развал", "body": "Кузов",
    "pneumo": "Пневмо и гайковёрты", "pullers": "Спецсъёмники", "measure": "Измерительный", "other": "Прочее",
}


class Tool(Entity):
    __tablename__ = "tools"
    category: Mapped[str | None] = mapped_column(String(20), default="personal", nullable=True)  # TOOL_CATEGORIES
    # Общий инструмент, выданный временно: под какой наряд и какую работу (пусто — не выдан / личный)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)
    inv_no: Mapped[str] = mapped_column(String(40), index=True)       # инвентарный номер (И-0001)
    serial: Mapped[str] = mapped_column(String(80), default="")       # серийный номер производителя
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")        # состав набора и т.п.
    cost: Mapped[Decimal] = mapped_column(MONEY, default=0)           # стоимость (для удержаний)
    photo: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(12), default="ok")
    holder_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)  # пусто = склад

    @property
    def cat(self) -> str:
        """Категория; у инструмента, заведённого до 10.11.2026, её нет — считаем личным."""
        return self.category if self.category in TOOL_CATEGORIES else "personal"

    @property
    def is_personal(self) -> bool:
        return self.cat == "personal"


class ToolMove(Entity):
    """История: кому выдан, у кого забран, списан."""
    __tablename__ = "tool_moves"
    tool_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tools.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # issue / return / writeoff / lost
    from_user: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    to_user: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)


class ToolCheck(Entity):
    __tablename__ = "tool_checks"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)  # чей инструмент проверяем
    kind: Mapped[str] = mapped_column(String(12), default="planned")
    status: Mapped[str] = mapped_column(String(10), default="draft")
    note: Mapped[str] = mapped_column(Text, default="")
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ToolCheckLine(Entity):
    __tablename__ = "tool_check_lines"
    check_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tool_checks.id", ondelete="CASCADE"), index=True)
    tool_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tools.id"))
    result: Mapped[str] = mapped_column(String(8), default="ok")
    comment: Mapped[str] = mapped_column(Text, default="")
    photo: Mapped[str] = mapped_column(String(255), default="")
    charge: Mapped[Decimal] = mapped_column(MONEY, default=0)          # удержание (по вине / утерян)
    charge_approved: Mapped[bool] = mapped_column(Boolean, default=False)
