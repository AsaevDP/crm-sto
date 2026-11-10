"""Справочник брендов запчастей (11.11.2026): добавили один раз — видят и выбирают все сотрудники."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Brand, PartCatalog

# Стартовый список: частые бренды на СТО (дальше правится в «Настройки → Бренды» или прямо в окне запчасти)
DEFAULT_BRANDS = [
    "Оригинал", "Bosch", "TRW", "Brembo", "ATE", "Ferodo", "Textar", "MANN", "Mahle", "Knecht", "Filtron", "Hengst",
    "NGK", "Denso", "Champion", "Beru", "SKF", "FAG", "INA", "SNR", "LuK", "Sachs", "Valeo", "Kayaba", "Monroe",
    "Bilstein", "Lemförder", "Febi", "Meyle", "Moog", "Delphi", "Gates", "Contitech", "Dayco", "Hepu", "Wahler",
    "Elring", "Victor Reinz", "Corteco", "Castrol", "Mobil", "Shell", "Liqui Moly", "Motul", "Total", "Varta",
    "Osram", "Philips", "Hella", "Pierburg", "Swag", "Ruville", "Japanparts", "Blue Print",
]


def norm(name: str) -> str:
    return " ".join((name or "").split())[:80]


def names(db: Session, branch_id) -> list[str]:
    return list(db.scalars(select(Brand.name).where(Brand.branch_id == branch_id).order_by(func.lower(Brand.name))))


def find(db: Session, branch_id, name: str) -> Brand | None:
    n = norm(name).lower()
    return db.scalar(select(Brand).where(Brand.branch_id == branch_id, func.lower(Brand.name) == n)) if n else None


def add(db: Session, branch_id, name: str) -> Brand | None:
    """Добавить бренд (если такой уже есть — без дубля, регистр не важен)."""
    name = norm(name)
    if not name:
        return None
    b = find(db, branch_id, name)
    if b is None:
        b = Brand(name=name)
        db.add(b)
        db.flush()
    return b


def seed(db: Session, branch_id):
    """Пустой справочник → стартовый список + бренды, уже встречающиеся в каталоге запчастей."""
    if db.scalar(select(Brand.id).where(Brand.branch_id == branch_id).limit(1)):
        return
    have = set()
    for n in DEFAULT_BRANDS + list(db.scalars(select(PartCatalog.brand).where(PartCatalog.brand != "").distinct())):
        k = norm(n).lower()
        if k and k not in have:
            have.add(k)
            db.add(Brand(name=norm(n), branch_id=branch_id))
    db.flush()


def rename(db: Session, b: Brand, new: str) -> str:
    new = norm(new)
    if not new:
        return "Пустое название"
    other = find(db, b.branch_id, new)
    if other and other.id != b.id:
        return f"«{new}» уже есть в списке"
    old = b.name
    b.name = new
    for c in db.scalars(select(PartCatalog).where(PartCatalog.brand == old)):
        c.brand = new
    return ""
