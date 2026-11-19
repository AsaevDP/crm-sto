"""Мойка (18.11.2026, брат): прайс услуг и боксы мойки.

Услуга — название, цена по классу машины (легковая / кроссовер / бус) и примерная длительность (для очереди
«≈ начнут / ≈ готово»). Править прайс — право wash.prices (менеджер, директор, зам, админ); мойщик только видит.
Бокс — место, где моют (как подъёмник у слесарей): «Мойка 1», «Детейлинг»."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, UTCDateTime

MONEY = Numeric(12, 2)


class WashService(Entity):
    __tablename__ = "wash_services"
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10), default="wash")      # wash — мойка, detail — детейлинг
    price_car: Mapped[Decimal] = mapped_column(MONEY, default=0)        # легковая
    price_suv: Mapped[Decimal] = mapped_column(MONEY, default=0)        # кроссовер / внедорожник
    price_bus: Mapped[Decimal] = mapped_column(MONEY, default=0)        # бус / микроавтобус
    minutes: Mapped[int] = mapped_column(Integer, default=30)           # сколько примерно занимает
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)         # скрытая не предлагается при приёмке


class WashQueueRequest(Entity):
    """Запрос мойщика «Изменить очередь» (19.11.2026, брат): мойщик просит поднять машину первой (взять вне очереди
    сам не может) → менеджер «Согласен» (машина встаёт первой) или «Отказать» с причиной; мойщику — ответ сообщением.
    status: new — ждёт менеджера, ok — согласовано, declined — отказано, cancelled — снят (машину уже взяли / выдали)."""
    __tablename__ = "wash_queue_requests"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    answer: Mapped[str] = mapped_column(Text, default="")     # причина отказа


class WashBox(Entity):
    __tablename__ = "wash_boxes"
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
