"""Склад (10.11.2026): дерево категорий, применимость (марка/модель/годы), штрихкоды наклеек."""
import uuid

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from ..models import PartCatalog, PartFit, StockCategory, StockItem
from ..models.stock import SECTIONS

# Стартовое дерево (потом правится на «Склад → Категории»)
DEFAULT_TREE = {
    "to": {
        "Масла": ["Моторные", "Трансмиссионные", "Гидравлика и ГУР"],
        "Фильтры": ["Масляные", "Воздушные", "Салонные", "Топливные"],
        "Свечи": ["Зажигания", "Накаливания"],
        "Жидкости": ["Антифриз", "Тормозная", "Омыватель"],
        "Ремни и ролики": ["ГРМ", "Приводные", "Ролики и натяжители"],
        "Щётки и лампы": [],
        "Расходники": ["Смазки и герметики", "Хомуты и крепёж"],
    },
    "repair": {
        "Двигатель": ["Прокладки и сальники", "ГРМ", "Поршневая", "Опоры двигателя"],
        "Ходовая и подвеска": ["Амортизаторы", "Рычаги и сайлентблоки", "Стойки стабилизатора", "Подшипники ступиц", "Пружины"],
        "Тормоза": ["Колодки", "Диски", "Суппорта", "Шланги и трубки"],
        "Рулевое": ["Наконечники", "Тяги", "Рейки и насосы"],
        "Трансмиссия": ["Сцепление", "ШРУСы и пыльники", "КПП"],
        "Охлаждение": ["Радиаторы", "Помпы", "Термостаты"],
        "Электрика": ["Датчики", "Генераторы и стартеры", "Аккумуляторы"],
        "Выхлоп": [],
        "Кузов и оптика": [],
        "Салон": [],
    },
    # 12.11.2026: химия для мойки и детейлинга
    "chem": {
        "Шампуни": ["Активная пена", "Ручная мойка"],
        "Полироли и воски": ["Полироли", "Воски и защита"],
        "Очистители": ["Кузов и битум", "Стёкла", "Двигатель", "Диски"],
        "Для салона": ["Пластик", "Кожа", "Ткань"],
        "Для шин и дисков": [],
        "Расходники для мойки": ["Микрофибра", "Губки и щётки"],
    },
}


# ---------- Штрихкод ----------
def barcode_for(item_id: uuid.UUID) -> str:
    """12 цифр «2…» из номера позиции: свой у каждой, без счётчика (внутренний диапазон, не EAN магазинов)."""
    return "2" + f"{item_id.int % 10 ** 11:011d}"


@event.listens_for(StockItem, "before_insert")
def _set_barcode(mapper, connection, target):
    if not target.barcode and target.id:
        target.barcode = barcode_for(target.id)


def backfill_barcodes(db: Session):
    """Позициям, заведённым до 10.11.2026, — штрихкод."""
    for i in db.scalars(select(StockItem).where(StockItem.barcode.is_(None))):
        i.barcode = barcode_for(i.id)


def by_barcode(db: Session, branch_id, code: str) -> StockItem | None:
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 12):
        return None
    return db.scalar(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.barcode == code))


# ---------- Категории ----------
def seed(db: Session, branch_id):
    """Первое открытие: стартовое дерево + категории, которые уже встречаются в справочнике."""
    # По разделам: новый раздел (химия, 12.11.2026) получает стартовое дерево и в уже работающей базе
    have_sec = set(db.scalars(select(StockCategory.section).where(StockCategory.branch_id == branch_id).distinct()))
    for sec, cats in DEFAULT_TREE.items():
        if sec in have_sec:
            continue
        for i, (name, subs) in enumerate(cats.items()):
            c = StockCategory(section=sec, name=name, sort=i, branch_id=branch_id)
            db.add(c)
            db.flush()
            for j, s in enumerate(subs):
                db.add(StockCategory(section=sec, name=s, parent_id=c.id, sort=j, branch_id=branch_id))
    db.flush()
    have = {(c.section, c.name) for c in db.scalars(select(StockCategory).where(StockCategory.parent_id.is_(None)))}
    for c in db.scalars(select(PartCatalog).where(PartCatalog.kind != "used", PartCatalog.category != "")):
        if c.section in SECTIONS and (c.section, c.category) not in have:
            db.add(StockCategory(section=c.section, name=c.category[:60], sort=99, branch_id=branch_id))
            have.add((c.section, c.category))
    db.flush()


def tree(db: Session, branch_id, section: str) -> list[dict]:
    """[{"c": категория, "subs": [подкатегории]}] по порядку."""
    rows = db.scalars(select(StockCategory).where(StockCategory.branch_id == branch_id, StockCategory.section == section)
                      .order_by(StockCategory.sort, StockCategory.name)).all()
    out = [{"c": c, "subs": []} for c in rows if not c.parent_id]
    idx = {x["c"].id: x for x in out}
    for c in rows:
        if c.parent_id in idx:
            idx[c.parent_id]["subs"].append(c)
    return out


def usage(db: Session, cat: StockCategory) -> int:
    """Сколько запчастей в справочнике стоит в этой категории / подкатегории."""
    q = select(PartCatalog.id).where(PartCatalog.branch_id == cat.branch_id, PartCatalog.section == cat.section)
    if cat.parent_id:
        parent = db.get(StockCategory, cat.parent_id)
        q = q.where(PartCatalog.category == parent.name, PartCatalog.subcategory == cat.name)
    else:
        q = q.where(PartCatalog.category == cat.name)
    return len(db.scalars(q).all())


def rename(db: Session, cat: StockCategory, new: str):
    """Переименовать — и у всех запчастей этой категории тоже."""
    old, new = cat.name, new.strip()[:60]
    if not new or new == old:
        return
    if cat.parent_id:
        parent = db.get(StockCategory, cat.parent_id)
        for p in db.scalars(select(PartCatalog).where(PartCatalog.section == cat.section, PartCatalog.category == parent.name,
                                                      PartCatalog.subcategory == old)):
            p.subcategory = new
    else:
        for p in db.scalars(select(PartCatalog).where(PartCatalog.section == cat.section, PartCatalog.category == old)):
            p.category = new
    cat.name = new


# ---------- Применимость ----------
def fits_map(db: Session, catalog_ids) -> dict:
    out: dict = {}
    for f in db.scalars(select(PartFit).where(PartFit.catalog_id.in_(list(catalog_ids))).order_by(PartFit.make, PartFit.model)):
        out.setdefault(f.catalog_id, []).append(f)
    return out


def fit_label(f: PartFit) -> str:
    years = ""
    if f.year_from and f.year_to:
        years = f" {f.year_from}–{f.year_to}"
    elif f.year_from:
        years = f" с {f.year_from}"
    elif f.year_to:
        years = f" до {f.year_to}"
    return f"{f.make} {f.model}".strip() + years


def makes_models(db: Session, branch_id) -> dict:
    """{марка: [модели]} — для фильтра склада."""
    out: dict = {}
    for f in db.scalars(select(PartFit).where(PartFit.branch_id == branch_id)):
        out.setdefault(f.make, set())
        if f.model:
            out[f.make].add(f.model)
    return {k: sorted(v) for k, v in sorted(out.items())}
