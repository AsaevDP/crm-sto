"""Разбор: доноры, б/у детали (код Р-0001), окупаемость донора, резерв под покупателя и продажа, экспорт для сайта.

Б/у деталь — обычная позиция склада (StockItem) с каталогом kind=used, поэтому остатки, ячейки,
резерв и подбор в наряд работают так же, как для новых запчастей.
"""
import csv
import io
import json
import secrets
import zipfile
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Donor, DonorPhoto, Location, PartCatalog, StockItem, StockPhoto, UsedSale
from ..models.razbor import CONDITIONS
from . import orders as osvc
from . import stock as ssvc

PHOTO_DIR = osvc.PHOTO_DIR / "razbor"
D0 = Decimal(0)


def _next(db: Session, branch_id, prefix: str, column, digits: int) -> str:
    codes = [c for c in db.scalars(select(column).where(column.like(f"{prefix}-%"))) if c]
    n = max((int(c.split("-")[1]) for c in codes if c.split("-")[1].isdigit()), default=0) + 1
    return f"{prefix}-{n:0{digits}d}"


def next_donor_code(db, branch_id):
    return _next(db, branch_id, "Д", Donor.code, 3)


def next_part_code(db, branch_id):
    return _next(db, branch_id, "Р", StockItem.code, 4)


def save_photo(data: bytes) -> str:
    ext = osvc._is_image(data)
    if not ext or len(data) > osvc.MAX_PHOTO_BYTES:
        return ""
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(8)}.{ext}"
    (PHOTO_DIR / name).write_bytes(data)
    return f"razbor/{name}"


# ---------- Деталь ----------
def add_part(db: Session, branch_id, donor: Donor | None, name: str, qty, price, condition: str, location_id,
             description: str = "", publish: bool = True, article: str = "", category: str = "",
             subcategory: str = "") -> StockItem:
    """Новая б/у деталь: каталог (kind=used) + позиция склада с кодом Р-0001 + приход."""
    from ..models.stock import guess_razbor_category
    cat = PartCatalog(name=name.strip()[:200], article=article.strip()[:80], kind="used",
                      category=(category.strip()[:60] or guess_razbor_category(name)),
                      subcategory=(subcategory or "").strip()[:60],
                      fits=f"{donor.make} {donor.model} {donor.year or ''}".strip() if donor else "")
    db.add(cat)
    db.flush()
    item = StockItem(catalog_id=cat.id, location_id=location_id, donor_id=donor.id if donor else None,
                     code=next_part_code(db, branch_id), condition=condition if condition in CONDITIONS else "good",
                     sale_price=ssvc.money(price), publish_on_site=publish, description=description.strip(),
                     donor_make=donor.make if donor else "", donor_model=donor.model if donor else "",
                     donor_year=donor.year if donor else None, donor_vin=donor.vin if donor else "")
    db.add(item)
    db.flush()
    ssvc.move(db, "in", item, Decimal(qty or 1), comment=f"Разбор {donor.code if donor else ''}".strip())
    return item


def reserve_for_buyer(db: Session, branch_id, item: StockItem, qty, price, buyer_name, buyer_phone, delivery,
                      pay_method="cash", comment="", np_address="", cod=False, channel="") -> UsedSale | str:
    b = ssvc.balances(db, branch_id, [item.id])[item.id]
    qty = Decimal(qty or 1)
    if b["free"] < qty:
        return "Свободного остатка нет — деталь уже в резерве или продана"
    s = UsedSale(stock_item_id=item.id, qty=qty, price=ssvc.money(price or item.sale_price * qty),
                 buyer_name=buyer_name.strip()[:160], buyer_phone=osvc.norm_phone(buyer_phone),
                 delivery=delivery, pay_method=pay_method, comment=comment.strip(),
                 np_address=(np_address or "").strip()[:200], cod=bool(cod), channel=channel or "")
    db.add(s)
    db.flush()
    ssvc.move(db, "reserve", item, qty, comment=f"Резерв под покупателя: {s.buyer_name or s.buyer_phone}")
    return s


def mark_sold(db: Session, s: UsedSale, price=None, ttn: str = "", pay_method: str | None = None):
    item = db.get(StockItem, s.stock_item_id)
    if s.status == "reserved":
        ssvc.move(db, "unreserve", item, s.qty, comment="Снят резерв — продано")
    if price is not None:
        s.price = ssvc.money(price)
    if pay_method:
        s.pay_method = pay_method
    s.ttn = (ttn or s.ttn).strip()[:40]
    ssvc.move(db, "sale", item, s.qty, comment=f"Продано: {s.buyer_name or 'покупатель'}{', ТТН ' + s.ttn if s.ttn else ''}")
    s.status, s.sold_at = "sold", now()


def sell_now(db: Session, branch_id, item: StockItem, qty, price, buyer_name, buyer_phone, delivery, pay_method, ttn=""):
    """Продажа сразу (без резерва), например покупатель приехал сам."""
    b = ssvc.balances(db, branch_id, [item.id])[item.id]
    if b["free"] < Decimal(qty or 1):
        return "Свободного остатка нет"
    # Статус «new»: резерва не было, поэтому mark_sold не будет его снимать
    s = UsedSale(stock_item_id=item.id, qty=Decimal(qty or 1), price=ssvc.money(price), buyer_name=buyer_name.strip()[:160],
                 buyer_phone=osvc.norm_phone(buyer_phone), delivery=delivery, pay_method=pay_method, status="new")
    db.add(s)
    db.flush()
    mark_sold(db, s, ttn=ttn)
    return s


def cancel_reserve(db: Session, s: UsedSale):
    if s.status == "reserved":
        ssvc.move(db, "unreserve", db.get(StockItem, s.stock_item_id), s.qty, comment="Резерв отменён")
        s.status, s.closed_at = "cancelled", now()


# ---------- Отправка почтой (11.11.2026) ----------
def ship(db: Session, s: UsedSale, ttn: str, cod: bool | None = None, paid: bool = False) -> str:
    """Резерв → «Отправлено»: деталь ушла со склада (списывается как продажа), ждём получение и деньги.
    Предоплата (paid) — деньги уже у нас. Наложенный платёж (cod) — деньги придут после получения."""
    if s.status != "reserved":
        return "Отправить можно только деталь в резерве"
    if not (ttn or "").strip() and s.delivery == "np":
        return "Впишите номер ТТН Новой Почты"
    item = db.get(StockItem, s.stock_item_id)
    ssvc.move(db, "unreserve", item, s.qty, comment="Снят резерв — отправлено")
    s.ttn = (ttn or "").strip()[:40]
    if cod is not None:
        s.cod = cod
    s.paid = bool(paid)
    ssvc.move(db, "sale", item, s.qty, comment=f"Отправлено: {s.buyer_name or 'покупатель'}{', ТТН ' + s.ttn if s.ttn else ''}")
    s.status, s.sent_at = "sent", now()
    return ""


def complete(db: Session, s: UsedSale, pay_method: str | None = None) -> str:
    """«Отправлено» → «Продано»: покупатель забрал, деньги получены (наложка пришла или была предоплата)."""
    if s.status != "sent":
        return "Закрыть можно только отправленную деталь"
    if pay_method:
        s.pay_method = pay_method
    s.paid, s.status, s.sold_at = True, "sold", now()
    return ""


def return_sale(db: Session, s: UsedSale, note: str = "") -> str:
    """Посылку не забрали / вернули: деталь снова на складе разбора (свободна)."""
    if s.status not in ("sent", "sold"):
        return "Вернуть можно отправленную или проданную деталь"
    item = db.get(StockItem, s.stock_item_id)
    ssvc.move(db, "return", item, s.qty, comment=f"Возврат от покупателя{': ' + note.strip() if note.strip() else ''}")
    s.status, s.closed_at = "returned", now()
    if note.strip():
        s.comment = (s.comment + "\n" if s.comment else "") + "Возврат: " + note.strip()
    return ""


def writeoff(db: Session, branch_id, item: StockItem, qty, reason: str) -> str:
    """Списать б/у деталь (разбилась, не годится). Только свободный остаток."""
    b = ssvc.balances(db, branch_id, [item.id])[item.id]
    qty = Decimal(qty or 1)
    if b["free"] < qty:
        return "Столько свободных нет — снимите резерв или проверьте остаток"
    if not reason.strip():
        return "Напишите причину списания"
    ssvc.move(db, "writeoff", item, qty, comment=f"Списано: {reason.strip()}")
    return ""


def deletable(db: Session, items) -> set:
    """Какие детали можно удалить совсем: были только приходы (заведена по ошибке), ни продаж, ни резервов."""
    from ..models import StockMovement
    ids = [i.id for i in items]
    if not ids:
        return set()
    busy = set(db.scalars(select(StockMovement.stock_item_id).where(StockMovement.stock_item_id.in_(ids),
                                                                     StockMovement.type != "in")))
    busy |= set(db.scalars(select(UsedSale.stock_item_id).where(UsedSale.stock_item_id.in_(ids))))
    return {i for i in ids if i not in busy}


def delete_part(db: Session, item: StockItem):
    """Удалить ошибочную деталь вместе с приходом и фото (в журнале остаётся запись об удалении)."""
    from ..models import StockMovement
    for m in db.scalars(select(StockMovement).where(StockMovement.stock_item_id == item.id)).all():
        db.delete(m)
    for p in db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id == item.id)).all():
        db.delete(p)
    cat_id = item.catalog_id
    db.delete(item)
    db.flush()
    if not db.scalar(select(StockItem.id).where(StockItem.catalog_id == cat_id)):
        from ..models import PurchaseLine, WoPart
        if not db.scalar(select(WoPart.id).where(WoPart.catalog_id == cat_id)) and \
                not db.scalar(select(PurchaseLine.id).where(PurchaseLine.catalog_id == cat_id)):
            db.delete(db.get(PartCatalog, cat_id))


def item_states(db: Session, branch_id, items, bal=None) -> dict:
    """Состояние каждой детали: в наличии / в резерве / отправлено / продано / в наряд СТО / списано / нет."""
    from ..models import StockMovement
    ids = [i.id for i in items]
    bal = bal or ssvc.balances(db, branch_id, ids)
    sent = set(db.scalars(select(UsedSale.stock_item_id).where(UsedSale.stock_item_id.in_(ids), UsedSale.status == "sent")))
    last_out = {}
    for sid, typ, wo_part in db.execute(select(StockMovement.stock_item_id, StockMovement.type, StockMovement.wo_part_id)
                                        .where(StockMovement.stock_item_id.in_(ids),
                                               StockMovement.type.in_(("sale", "issue", "writeoff")))
                                        .order_by(StockMovement.created_at)):
        last_out[sid] = "service" if typ == "issue" else ("writeoff" if typ == "writeoff" else "sold")
    out = {}
    for i in items:
        b = bal[i.id]
        if b["on_hand"] > 0:
            out[i.id] = "stock" if b["free"] > 0 else "reserved"
        elif i.id in sent:
            out[i.id] = "sent"
        else:
            out[i.id] = last_out.get(i.id, "out")
    return out


# ---------- Справочники: марки → модели, категории → подкатегории (11.11.2026) ----------
SUBCATS = {
    "Двигатель и навесное": ["Двигатель в сборе", "ГБЦ и клапана", "Турбина", "Генератор", "Стартер", "Впуск и выпуск", "Топливная система"],
    "КПП и привод": ["Коробка передач", "Раздатка", "Полуоси и ШРУСы", "Кардан", "Сцепление"],
    "Ходовая и подвеска": ["Рычаги", "Амортизаторы и стойки", "Ступицы", "Подрамник", "Пружины"],
    "Тормоза": ["Суппорты", "Диски", "Усилитель и ГТЦ", "ABS"],
    "Кузов": ["Двери", "Капот", "Бамперы", "Крылья", "Крышка багажника", "Пороги и панели"],
    "Оптика": ["Фары", "Фонари", "ПТФ"],
    "Салон": ["Сиденья", "Торпедо и панели", "Руль и подушки", "Обшивки"],
    "Электрика и блоки": ["Блоки управления", "Проводка", "Датчики", "Мультимедиа"],
}


def seed_refs(db: Session, branch_id):
    """Стартовые справочники: категории из списка + подкатегории; марки и модели — из уже заведённых доноров."""
    from ..models import RazborCategory, RazborMake, RazborModel
    from ..models.stock import RAZBOR_CATEGORIES
    if not db.scalar(select(RazborCategory.id).where(RazborCategory.branch_id == branch_id)):
        for n, name in enumerate(RAZBOR_CATEGORIES, 1):
            c = RazborCategory(branch_id=branch_id, name=name, sort=n)
            db.add(c)
            db.flush()
            for k, sub in enumerate(SUBCATS.get(name, []), 1):
                db.add(RazborCategory(branch_id=branch_id, name=sub, parent_id=c.id, sort=k))
    if not db.scalar(select(RazborMake.id).where(RazborMake.branch_id == branch_id)):
        pairs = set(db.execute(select(Donor.make, Donor.model).where(Donor.branch_id == branch_id)).all())
        pairs |= set(db.execute(select(StockItem.donor_make, StockItem.donor_model).where(
            StockItem.branch_id == branch_id, StockItem.donor_make != "")).all())
        for make, model in sorted(pairs):
            ensure_make_model(db, branch_id, make, model)
    db.flush()


def ensure_make_model(db: Session, branch_id, make: str, model: str = ""):
    """Марка и модель есть в справочнике (если нет — добавить). Регистр не важен."""
    from ..models import RazborMake, RazborModel
    make, model = (make or "").strip()[:60], (model or "").strip()[:60]
    if not make:
        return None, None
    mk = next((m for m in db.scalars(select(RazborMake).where(RazborMake.branch_id == branch_id))
               if m.name.lower() == make.lower()), None)
    if mk is None:
        mk = RazborMake(branch_id=branch_id, name=make)
        db.add(mk)
        db.flush()
    md = None
    if model:
        md = next((m for m in db.scalars(select(RazborModel).where(RazborModel.make_id == mk.id))
                   if m.name.lower() == model.lower()), None)
        if md is None:
            md = RazborModel(branch_id=branch_id, make_id=mk.id, name=model)
            db.add(md)
            db.flush()
    return mk, md


def tree(db: Session, branch_id) -> dict:
    """{"makes": [{m, models: [...]}], "cats": [{c, subs: [...]}]} — для форм и фильтров."""
    from ..models import RazborCategory, RazborMake, RazborModel
    makes = db.scalars(select(RazborMake).where(RazborMake.branch_id == branch_id).order_by(RazborMake.name)).all()
    models = db.scalars(select(RazborModel).where(RazborModel.branch_id == branch_id).order_by(RazborModel.name)).all()
    cats = db.scalars(select(RazborCategory).where(RazborCategory.branch_id == branch_id)
                      .order_by(RazborCategory.sort, RazborCategory.name)).all()
    return {"makes": [{"m": m, "models": [x for x in models if x.make_id == m.id]} for m in makes],
            "cats": [{"c": c, "subs": [x for x in cats if x.parent_id == c.id]} for c in cats if not c.parent_id]}


# ---------- Окупаемость донора ----------
@dataclass
class DonorEconomy:
    invested: Decimal       # покупка + расходы
    sold: Decimal           # продано через разбор
    used_in_orders: Decimal # ушло в наряды (по цене продажи)
    on_shelf: Decimal       # ещё лежит по ценам продажи
    parts_total: int
    parts_left: int

    @property
    def earned(self) -> Decimal:
        return self.sold + self.used_in_orders

    @property
    def profit(self) -> Decimal:
        return self.earned - self.invested

    @property
    def pct(self) -> int:
        return int(self.earned / self.invested * 100) if self.invested else 0


def economy(db: Session, branch_id, donor: Donor) -> DonorEconomy:
    items = db.scalars(select(StockItem).where(StockItem.donor_id == donor.id)).all()
    bal = ssvc.balances(db, branch_id, [i.id for i in items])
    sold = db.scalar(select(func.sum(UsedSale.price)).where(UsedSale.stock_item_id.in_([i.id for i in items]),
                                                            UsedSale.status == "sold")) or D0
    from ..models import StockMovement, WoPart
    used = D0
    for m in db.scalars(select(StockMovement).where(StockMovement.stock_item_id.in_([i.id for i in items]),
                                                    StockMovement.type == "issue", StockMovement.wo_part_id.is_not(None))):
        p = db.get(WoPart, m.wo_part_id)
        if p:
            used += p.price * m.qty
    on_shelf = sum((i.sale_price * max(bal[i.id]["on_hand"], D0) for i in items), D0)
    return DonorEconomy(invested=ssvc.money(donor.purchase_price + donor.expenses), sold=ssvc.money(sold),
                        used_in_orders=ssvc.money(used), on_shelf=ssvc.money(on_shelf), parts_total=len(items),
                        parts_left=sum(1 for i in items if bal[i.id]["on_hand"] > 0))


# ---------- Экспорт для сайта ----------
COLUMNS = ["Код", "Название", "Марка", "Модель", "Год", "Двигатель", "КПП", "Кузов", "Состояние", "Цена, грн",
           "Количество", "Описание", "Донор", "Фото", "Категория"]


def export_rows(db: Session, branch_id) -> list[dict]:
    """Только детали с флажком «на сайт» и свободным остатком."""
    items = db.scalars(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.publish_on_site.is_(True))
                       .order_by(StockItem.code)).all()
    bal = ssvc.balances(db, branch_id, [i.id for i in items])
    rows = []
    for i in items:
        free = bal[i.id]["free"]
        if free <= 0:
            continue
        cat = db.get(PartCatalog, i.catalog_id)
        donor = db.get(Donor, i.donor_id) if i.donor_id else None
        photos = db.scalars(select(StockPhoto).where(StockPhoto.stock_item_id == i.id).order_by(StockPhoto.created_at)).all()
        rows.append({
            "Код": i.code, "Название": cat.name, "Категория": cat.category, "Марка": i.donor_make, "Модель": i.donor_model,
            "Год": i.donor_year or "", "Двигатель": donor.engine if donor else "", "КПП": donor.gearbox if donor else "",
            "Кузов": donor.body if donor else "", "Состояние": CONDITIONS.get(i.condition, i.condition),
            "Цена, грн": float(i.sale_price), "Количество": float(free), "Описание": i.description,
            "Донор": donor.code if donor else "",
            "Фото": ", ".join(f"{i.code}_{n + 1}.{p.file.rsplit('.', 1)[-1]}" for n, p in enumerate(photos)),
            "_photos": [p.file for p in photos],
        })
    return rows


def export_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, delimiter=";", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel сразу понял кириллицу


def export_xlsx(rows) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Разбор"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r[k] for k in COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMN", (9, 34, 12, 14, 7, 16, 12, 12, 18, 11, 11, 40, 8, 30)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def export_json(rows) -> bytes:
    data = [{"code": r["Код"], "name": r["Название"], "make": r["Марка"], "model": r["Модель"], "year": r["Год"],
             "engine": r["Двигатель"], "gearbox": r["КПП"], "body": r["Кузов"], "condition": r["Состояние"],
             "price_uah": r["Цена, грн"], "qty": r["Количество"], "description": r["Описание"],
             "photos": [x.strip() for x in r["Фото"].split(",") if x.strip()]} for r in rows]
    return json.dumps({"items": data}, ensure_ascii=False, indent=2).encode("utf-8")


def export_zip(rows) -> bytes:
    """Всё в одном архиве: Excel, CSV, JSON и папка photos с именами «Р-0001_1.jpg»."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("razbor.xlsx", export_xlsx(rows))
        z.writestr("razbor.csv", export_csv(rows))
        z.writestr("razbor.json", export_json(rows))
        for r in rows:
            for n, rel in enumerate(r["_photos"]):
                f = osvc.PHOTO_DIR / rel
                if f.exists():
                    z.write(f, f"photos/{r['Код']}_{n + 1}.{rel.rsplit('.', 1)[-1]}")
    return out.getvalue()
