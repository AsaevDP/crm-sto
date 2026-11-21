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
# 12.11.2026: «Готово» = в резерве + упаковано (packed_at): готово к выдаче (самовывоз) или к отправке (служба доставки)
SALE_STATUS = {"reserved": "В резерве", "sent": "Отправлено", "sold": "Продано", "returned": "Возврат",
               "cancelled": "Отменено"}
# Виды служб доставки: почта (ТТН обязателен) или курьер (свой / организация, ТТН не нужен)
CARRIER_KINDS = {"post": "Служба доставки (ТТН)", "courier": "Курьер"}
DEFAULT_CARRIERS = [("Нова Пошта", "post"), ("Укрпошта", "post"), ("Meest", "post"), ("Личный курьер", "courier")]
DELIVERY = {"pickup": "Самовывоз", "np": "Нова Пошта", "other": "Другая доставка"}
# Откуда пришёл покупатель (11.11.2026) — чтобы видеть, где продаётся лучше
CHANNELS = {"phone": "Звонок", "site": "Сайт", "olx": "OLX", "social": "Соцсети", "walkin": "Пришёл сам", "other": "Другое"}
# Состояние детали на складе разбора (считается по движениям и продажам)
ITEM_STATES = {"unapproved": "Не утверждено", "declined": "Отказано", "stock": "В наличии", "reserved": "В резерве",
               "sent": "Отправлено", "sold": "Продано", "service": "В наряд СТО", "writeoff": "Списано", "out": "Нет в наличии"}


class RazborPrice(Entity):
    """Утверждение цены детали разбора (18.11.2026, брат). kind: new — новая деталь (или повторно после отказа),
    change — кладовщик разбора просит изменить утверждённую цену. Решают директор, зам, админ (право razbor.price):
    утвердить (цену можно поправить) или отказать с причиной."""
    __tablename__ = "razbor_prices"
    stock_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_items.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="new")            # new / change
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)                # какую цену просит кладовщик
    old_price: Mapped[Decimal] = mapped_column(MONEY, default=0)            # для change — какая была
    note: Mapped[str] = mapped_column(Text, default="")                     # пояснение кладовщика
    repeat: Mapped[bool] = mapped_column(Boolean, default=False)            # отправлено повторно после отказа
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)   # pending / approved / declined
    final_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    answer: Mapped[str] = mapped_column(Text, default="")                   # причина отказа
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


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
    # 12.11.2026: служба доставки (справочник), упаковано — кто/когда, где лежит готовая посылка/деталь
    carrier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    packed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    packed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    place: Mapped[str | None] = mapped_column(String(60), default="", nullable=True)
    # 22.11.2026 (брат): продажа новой детали из «На сайт → Новые» (SiteNew); у б/у — пусто
    site_new_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)


class Carrier(Entity):
    """Служба доставки (12.11.2026): Нова Пошта, Укрпошта, Meest, свой курьер, организация — добавляются в справочнике."""
    __tablename__ = "carriers"
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10), default="post")   # CARRIER_KINDS
    phone: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class SiteNew(Entity):
    """«На сайт → Новые» (22.11.2026, брат): новая запчасть со склада на продажу — от неё отказался клиент (запчастист
    «Продать» в «Лишнем после отказов») или её выставили из карточки склада («На продажу»). Цена — та, что была для
    клиента (запчастист с наценкой), кладовщик разбора её не меняет; на сайт — после утверждения директором / замом.
    Деталь остаётся обычной позицией склада: её можно взять в наряд — тогда из продажи она уходит сама (нет свободного
    остатка). state: pending — ждёт утверждения, approved — утверждена, declined — отказано (answer — причина).
    status: on — выставлена, off — снята с продажи."""
    __tablename__ = "site_new"
    catalog_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("parts_catalog.id"), index=True)
    code: Mapped[str] = mapped_column(String(20), default="", index=True)        # Н-0001 — для сайта
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=1)             # сколько выставили на продажу
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)                    # за 1 шт — цена для клиента
    state: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    answer: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="on", index=True)
    source: Mapped[str] = mapped_column(String(10), default="refused")          # refused / manual
    leftover_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")                       # почему клиент отказался
    description: Mapped[str] = mapped_column(Text, default="")                  # для объявления на сайте
    publish: Mapped[bool] = mapped_column(Boolean, default=True)                # показывать на сайте


class SiteNewPhoto(Entity):
    __tablename__ = "site_new_photos"
    site_new_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("site_new.id", ondelete="CASCADE"), index=True)
    file: Mapped[str] = mapped_column(String(255))
