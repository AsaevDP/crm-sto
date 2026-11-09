"""Разбор: доноры (авто на разбор) и продажи б/у деталей (шаг 7).
Сами б/у детали — это StockItem с kind=used в каталоге (общий склад), см. models/stock.py."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity, UTCDateTime

MONEY = Numeric(12, 2)

DONOR_STATUS = {"dismantling": "На разборке", "done": "Разобран", "scrapped": "Утилизирован"}
CONDITIONS = {"excellent": "Отличное", "good": "Хорошее", "fair": "Удовлетворительное", "repair": "Под восстановление"}
SALE_STATUS = {"reserved": "В резерве", "sold": "Продано", "cancelled": "Отменено"}
DELIVERY = {"pickup": "Самовывоз", "np": "Нова Пошта", "other": "Другая доставка"}


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
