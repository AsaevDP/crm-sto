"""Разбор: доноры (авто на разбор) и продажи б/у деталей (шаг 7).
Сами б/у детали — это StockItem с kind=used в каталоге (общий склад), см. models/stock.py."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, UTCDateTime

MONEY = Numeric(12, 2)

DONOR_STATUS = {"dismantling": "На разборке", "done": "Разобран", "scrapped": "Утилизирован"}
CONDITIONS = {"excellent": "Отличное", "good": "Хорошее", "fair": "Удовлетворительное", "repair": "Под восстановление"}
SALE_STATUS = {"reserved": "В резерве", "sent": "Отправлено", "sold": "Продано", "returned": "Возврат",
               "cancelled": "Отменено"}
DELIVERY = {"pickup": "Самовывоз", "np": "Нова Пошта", "other": "Другая доставка"}
# Откуда пришёл покупатель (11.11.2026) — чтобы видеть, где продаётся лучше
CHANNELS = {"phone": "Звонок", "site": "Сайт", "olx": "OLX", "social": "Соцсети", "walkin": "Пришёл сам", "other": "Другое"}
# Состояние детали на складе разбора (считается по движениям и продажам)
ITEM_STATES = {"stock": "В наличии", "reserved": "В резерве", "sent": "Отправлено", "sold": "Продано",
               "service": "В наряд СТО", "writeoff": "Списано", "out": "Нет в наличии"}


class RazborMake(Entity):
    """Справочник марок разбора (11.11.2026): марка → модели. Можно добавлять и удалять."""
    __tablename__ = "razbor_makes"
    name: Mapped[str] = mapped_column(String(60))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class RazborModel(Entity):
    __tablename__ = "razbor_models"
    make_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("razbor_makes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    years: Mapped[str] = mapped_column(String(40), default="")     # «2015–2020», для подсказки


class RazborCategory(Entity):
    """Категории деталей разбора: раздел → подкатегория (как дерево склада). parent_id пусто — раздел."""
    __tablename__ = "razbor_categories"
    name: Mapped[str] = mapped_column(String(60))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("razbor_categories.id", ondelete="CASCADE"),
                                                        nullable=True, index=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Donor(Entity):
    __tablename__ = "donors"
    code: Mapped[str] = mapped_column(String(20), index=True)          # Д-001
    make: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vin: Mapped[str] = mapped_column(String(32), default="")
    engine: Mapped[str] = mapped_column(String(60), default="")         # «3.0 дизель N57»
    gearbox: Mapped[str] = mapped_column(String(60), default="")        # «АКПП 8HP»
    body: Mapped[str] = mapped_column(String(60), default="")           # «кроссовер»
    color: Mapped[str] = mapped_column(String(40), default="")
    mileage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bought_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    expenses: Mapped[Decimal] = mapped_column(MONEY, default=0)         # доставка, разборка, утилизация
    expenses_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="dismantling")
    notes: Mapped[str] = mapped_column(Text, default="")


class DonorPhoto(Entity):
    __tablename__ = "donor_photos"
    donor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("donors.id", ondelete="CASCADE"), index=True)
    file: Mapped[str] = mapped_column(String(255))


class UsedSale(Entity):
    """Продажа б/у детали (с сайта, по телефону): резерв под покупателя → продано."""
    __tablename__ = "used_sales"
    stock_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_items.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=1)
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)            # за всё количество
    buyer_name: Mapped[str] = mapped_column(String(160), default="")
    buyer_phone: Mapped[str] = mapped_column(String(40), default="")
    delivery: Mapped[str] = mapped_column(String(10), default="pickup")
    ttn: Mapped[str] = mapped_column(String(40), default="")            # номер ТТН Новой Почты
    pay_method: Mapped[str] = mapped_column(String(10), default="cash")
    status: Mapped[str] = mapped_column(String(10), default="reserved")
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    # 11.11.2026: отправки почтой. Резерв → «Отправлено» (деталь ушла со склада, ждём получение и деньги)
    # → «Продано» (получили деньги) или «Возврат» (деталь вернулась на склад).
    np_address: Mapped[str | None] = mapped_column(String(200), default="", nullable=True)  # город, отделение
    cod: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)         # наложенный платёж
    paid: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)        # деньги получены
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)       # возврат / отмена
    channel: Mapped[str | None] = mapped_column(String(10), default="", nullable=True)    # CHANNELS
