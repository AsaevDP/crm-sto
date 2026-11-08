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


class PartCatalog(Entity):
    __tablename__ = "parts_catalog"
    article: Mapped[str] = mapped_column(String(80), default="", index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    brand: Mapped[str] = mapped_column(String(80), default="")
    fits: Mapped[str] = mapped_column(Text, default="")  # применимость: марки/модели, текстом
    kind: Mapped[str] = mapped_column(String(10), default="new")  # new / used
    revision: Mapped[str] = mapped_column(String(40), default="")  # ревизия детали (внутреннее)
    unit: Mapped[str] = mapped_column(String(10), default="шт")


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
