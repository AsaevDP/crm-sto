"""Склад и закупки. Остаток не хранится — считается по stock_movements."""
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Entity

MONEY = Numeric(12, 2)

MOVEMENT_TYPES = {
    "in": "приход", "reserve": "резерв", "unreserve": "снятие резерва", "issue": "выдача",
    "return": "возврат", "writeoff": "списание", "sale": "продажа (разбор)", "adjust": "инвентаризация",
}


# Склад делится на два основных раздела, внутри — категории (список можно дополнять: поле свободное)
SECTIONS = {"to": "ТО", "repair": "Запчасти для ремонта"}
CATEGORIES = {
    "to": ["Масла", "Фильтры", "Свечи", "Жидкости", "Ремни и ролики", "Щётки и лампы", "Расходники"],
    "repair": ["Двигатель", "Ходовая и подвеска", "Тормоза", "Рулевое", "Трансмиссия", "Охлаждение",
               "Электрика", "Выхлоп", "Кузов и оптика", "Салон"],
}
# Категории разбора (б/у детали)
RAZBOR_CATEGORIES = ["Двигатель и навесное", "КПП и привод", "Ходовая и подвеска", "Тормоза", "Рулевое",
                     "Кузов", "Оптика", "Стёкла и зеркала", "Салон", "Электрика и блоки", "Охлаждение и климат",
                     "Диски и шины", "Выхлоп", "Прочее"]
_RAZBOR_WORDS = [("двигат", "Двигатель и навесное"), ("мотор", "Двигатель и навесное"), ("турбин", "Двигатель и навесное"),
                 ("генератор", "Электрика и блоки"), ("стартер", "Электрика и блоки"), ("блок", "Электрика и блоки"),
                 ("кпп", "КПП и привод"), ("короб", "КПП и привод"), ("акпп", "КПП и привод"), ("привод", "КПП и привод"),
                 ("кардан", "КПП и привод"), ("рычаг", "Ходовая и подвеска"), ("амортиз", "Ходовая и подвеска"),
                 ("стойк", "Ходовая и подвеска"), ("ступиц", "Ходовая и подвеска"), ("суппорт", "Тормоза"),
                 ("рулев", "Рулевое"), ("фара", "Оптика"), ("фонарь", "Оптика"), ("птф", "Оптика"),
                 ("зеркал", "Стёкла и зеркала"), ("стекл", "Стёкла и зеркала"), ("дверь", "Кузов"), ("капот", "Кузов"),
                 ("бампер", "Кузов"), ("крыло", "Кузов"), ("крышка багаж", "Кузов"), ("сиден", "Салон"),
                 ("торпед", "Салон"), ("руль", "Салон"), ("панел", "Салон"), ("радиатор", "Охлаждение и климат"),
                 ("компрессор кондиц", "Охлаждение и климат"), ("диск", "Диски и шины"), ("шин", "Диски и шины"),
                 ("глушит", "Выхлоп"), ("катализ", "Выхлоп")]


def guess_razbor_category(name: str) -> str:
    n = (name or "").lower()
    for word, cat in _RAZBOR_WORDS:
        if word in n:
            return cat
    return "Прочее"


_TO_WORDS = ("масл", "фильтр", "свеч", "антифриз", "тосол", "жидкост", "омывай", "ремень", "ремня", "ролик",
             "щётк", "щетк", "дворник", "лампа", "смазк", "присадк", "oil", "filter")


def guess_section(name: str) -> str:
    """Раздел по названию: расходники ТО — «ТО», остальное — «Запчасти для ремонта»."""
    n = (name or "").lower()
    return "to" if any(w in n for w in _TO_WORDS) else "repair"


def guess_category(name: str, section: str) -> str:
    n = (name or "").lower()
    rules = {
        "to": [("фильтр", "Фильтры"), ("масл", "Масла"), ("свеч", "Свечи"), ("антифриз", "Жидкости"),
               ("жидкост", "Жидкости"), ("ремень", "Ремни и ролики"), ("ремня", "Ремни и ролики"),
               ("ролик", "Ремни и ролики"), ("щётк", "Щётки и лампы"), ("щетк", "Щётки и лампы"), ("лампа", "Щётки и лампы")],
        "repair": [("колод", "Тормоза"), ("тормоз", "Тормоза"), ("диск торм", "Тормоза"), ("амортиз", "Ходовая и подвеска"),
                   ("рычаг", "Ходовая и подвеска"), ("сайлент", "Ходовая и подвеска"), ("шаров", "Ходовая и подвеска"),
                   ("стойк", "Ходовая и подвеска"), ("пружин", "Ходовая и подвеска"), ("подшип", "Ходовая и подвеска"),
                   ("рулев", "Рулевое"), ("наконечник", "Рулевое"), ("тяга", "Рулевое"), ("сцеплен", "Трансмиссия"),
                   ("шрус", "Трансмиссия"), ("радиатор", "Охлаждение"), ("помпа", "Охлаждение"), ("термостат", "Охлаждение"),
                   ("датчик", "Электрика"), ("генератор", "Электрика"), ("стартер", "Электрика"), ("аккумул", "Электрика"),
                   ("глушит", "Выхлоп"), ("катализ", "Выхлоп"), ("фара", "Кузов и оптика"), ("бампер", "Кузов и оптика"),
                   ("прокладк", "Двигатель"), ("поршн", "Двигатель"), ("грм", "Двигатель")],
    }
    for word, cat in rules.get(section, []):
        if word in n:
            return cat
    return ""


class PartCatalog(Entity):
    __tablename__ = "parts_catalog"
    article: Mapped[str] = mapped_column(String(80), default="", index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    brand: Mapped[str] = mapped_column(String(80), default="")
    fits: Mapped[str] = mapped_column(Text, default="")  # применимость: марки/модели, текстом
    kind: Mapped[str] = mapped_column(String(10), default="new")  # new / used
    revision: Mapped[str] = mapped_column(String(40), default="")  # ревизия детали (внутреннее)
    unit: Mapped[str] = mapped_column(String(10), default="шт")
    # Раздел склада: to — ТО, repair — запчасти для ремонта (у б/у не используется)
    section: Mapped[str] = mapped_column(String(10), default="", index=True)
    # Категория внутри раздела (или категория разбора для б/у)
    category: Mapped[str] = mapped_column(String(60), default="", index=True)
    subcategory: Mapped[str | None] = mapped_column(String(60), default="", nullable=True, index=True)


class StockCategory(Entity):
    """Дерево категорий склада (10.11.2026): раздел → категория → подкатегория. Правится на «Склад → Категории».
    У запчасти в справочнике хранится название категории/подкатегории (переименование меняет и их)."""
    __tablename__ = "stock_categories"
    section: Mapped[str] = mapped_column(String(10), index=True)       # to / repair
    name: Mapped[str] = mapped_column(String(60))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stock_categories.id", ondelete="CASCADE"), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class PartFit(Entity):
    """Применимость запчасти: марка, модель, годы (для фильтра склада по марке и модели)."""
    __tablename__ = "part_fits"
    catalog_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("parts_catalog.id", ondelete="CASCADE"), index=True)
    make: Mapped[str] = mapped_column(String(60), index=True)
    model: Mapped[str] = mapped_column(String(60), default="")
    year_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year_to: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Location(Entity):
    __tablename__ = "locations"
    rack: Mapped[str] = mapped_column(String(10))
    shelf: Mapped[str] = mapped_column(String(10), default="")
    place: Mapped[str] = mapped_column(String(10), default="")
    label: Mapped[str] = mapped_column(String(40), default="")  # «B / 2 / 3»


class StockItem(Entity):
    __tablename__ = "stock_items"
    catalog_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("parts_catalog.id"))
    location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("locations.id"), nullable=True)
    cost: Mapped[Decimal] = mapped_column(MONEY, default=0)
    # Для б/у (разбор)
    donor_make: Mapped[str] = mapped_column(String(60), default="")
    donor_model: Mapped[str] = mapped_column(String(60), default="")
    donor_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    donor_vin: Mapped[str] = mapped_column(String(32), default="")
    condition: Mapped[str] = mapped_column(String(120), default="")
    sale_price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    publish_on_site: Mapped[bool] = mapped_column(Boolean, default=False)
    # Разбор (б/у): донор, код для бирки и сайта, описание для объявления
    donor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("donors.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(20), default="", index=True)   # Р-0001
    description: Mapped[str] = mapped_column(Text, default="")
    # Штрихкод наклейки (10.11.2026): 12 цифр «20…», у каждой позиции — новой и б/у. Сканер «печатает» его в поиск.
    barcode: Mapped[str | None] = mapped_column(String(20), default=None, nullable=True, index=True)


class StockPhoto(Entity):
    __tablename__ = "stock_photos"
    stock_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_items.id", ondelete="CASCADE"))
    file: Mapped[str] = mapped_column(String(255))


class Supplier(Entity):
    __tablename__ = "suppliers"
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class Purchase(Entity):
    __tablename__ = "purchases"
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("suppliers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / ordered / partial / arrived
    supplier_invoice: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(Text, default="")


class PurchaseLine(Entity):
    __tablename__ = "purchase_lines"
    purchase_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("purchases.id", ondelete="CASCADE"))
    catalog_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("parts_catalog.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    wo_part_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_parts.id"), nullable=True)  # пусто = на склад
    # Купили больше, чем нужно наряду: qty — всего, под наряд уходит wo_part.qty, остальное — на склад
    received: Mapped[bool] = mapped_column(Boolean, default=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("locations.id"), nullable=True)


class StockMovement(Entity):
    __tablename__ = "stock_movements"
    type: Mapped[str] = mapped_column(String(12), index=True)
    stock_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_items.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # +/−
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_orders.id"), nullable=True)
    wo_part_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("wo_parts.id"), nullable=True)
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("purchases.id"), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")


class IssueRequest(Entity):
    __tablename__ = "issue_requests"
    work_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"))
    status: Mapped[str] = mapped_column(String(10), default="new")  # new / issued / cancelled
    comment: Mapped[str] = mapped_column(Text, default="")
    issued_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)


class IssueRequestLine(Entity):
    __tablename__ = "issue_request_lines"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("issue_requests.id", ondelete="CASCADE"))
    wo_part_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("wo_parts.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    issued_qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
