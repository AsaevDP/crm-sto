"""Учёт инструмента сотрудников (шаг 6а, BRIEF 8а)."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
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
    "personal": "Личный", "spare": "Запасной набор", "to": "Для ТО", "engine": "Двигатель", "chassis": "Ходовая", "brakes": "Тормоза",
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
    place: Mapped[str | None] = mapped_column(String(80), default="", nullable=True)  # «Шкаф 1 · полка 2» (11.11.2026)
    # Полка шкафа из справочника (11.11.2026). place — её подпись текстом (для старых экранов и печати)
    shelf_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tool_shelves.id", ondelete="SET NULL"), nullable=True)

    @property
    def cat(self) -> str:
        """Категория; у инструмента, заведённого до 10.11.2026, её нет — считаем личным."""
        return self.category if self.category in TOOL_CATEGORIES else "personal"

    @property
    def is_personal(self) -> bool:
        """Личный или запасной набор: выдаётся сотруднику без наряда (запасной — взамен сломанного личного)."""
        return self.cat in ("personal", "spare")

    @property
    def is_spare(self) -> bool:
        return self.cat == "spare"


class ToolCabinet(Entity):
    """Шкаф для инструмента (11.11.2026). Полки — ToolShelf."""
    __tablename__ = "tool_cabinets"
    name: Mapped[str] = mapped_column(String(60))
    note: Mapped[str] = mapped_column(String(200), default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)


class ToolShelf(Entity):
    __tablename__ = "tool_shelves"
    cabinet_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tool_cabinets.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))     # «Полка 1», «Ящик 2»
    sort: Mapped[int] = mapped_column(Integer, default=0)


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


TOOL_REQ_KINDS = {"issue": "Просит выдать", "return": "Сдаёт"}


class ToolRequest(Entity):
    """Запрос по общему инструменту (11.11.2026): слесарь просит выдать под наряд или сдаёт обратно.
    У кладовщика всплывает окошко (как с запчастями): «Выдать» / «Принял»."""
    __tablename__ = "tool_requests"
    tool_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tools.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)   # кто просит / сдаёт
    kind: Mapped[str] = mapped_column(String(8), default="issue")                          # issue / return
    status: Mapped[str] = mapped_column(String(8), default="new", index=True)              # new / done / cancel
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    wo_work_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_works.id"), nullable=True)
    comment: Mapped[str] = mapped_column(String(300), default="")
    done_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result: Mapped[str] = mapped_column(String(8), default="")       # при возврате: ok / broken
    result_note: Mapped[str] = mapped_column(String(300), default="")


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
