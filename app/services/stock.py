"""Склад: остатки считаются ТОЛЬКО по движениям (stock_movements), отдельной цифры остатка нет.

на складе  = приход + возврат − выдача − списание − продажа ± инвентаризация
в резерве  = резерв − снятие резерва − выдача по резерву
свободно   = на складе − в резерве
"""
import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (IssueRequest, IssueRequestLine, Location, PartCatalog, Purchase, PurchaseLine, StockItem,
                      StockMovement, WoPart, WorkOrder)
from . import orders as osvc

D0 = Decimal("0")
ON_HAND_SIGN = {"in": 1, "return": 1, "issue": -1, "writeoff": -1, "sale": -1, "adjust": 1}
PART_STATUS_LABEL = {"need": "нужна", "ordered": "заказана", "arrived": "пришла", "reserved": "в резерве",
                     "issued": "выдана", "installed": "установлена", "returned": "возврат"}


def money(x) -> Decimal:
    return Decimal(x or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sale_price(cost, markup_pct) -> Decimal:
    """Цена продажи = закупка × (1 + наценка%)."""
    return money(Decimal(cost or 0) * (Decimal(100) + Decimal(markup_pct or 0)) / Decimal(100))


# ---------- Остатки ----------
def balances(db: Session, branch_id, item_ids=None) -> dict:
    """{stock_item_id: {"on_hand", "reserved", "free"}}"""
    has_part = StockMovement.wo_part_id.is_not(None)  # выдача «из резерва» уменьшает и резерв
    q = select(StockMovement.stock_item_id, StockMovement.type, has_part, func.sum(StockMovement.qty)) \
        .where(StockMovement.branch_id == branch_id) \
        .group_by(StockMovement.stock_item_id, StockMovement.type, has_part)
    if item_ids is not None:
        q = q.where(StockMovement.stock_item_id.in_(list(item_ids)))
    out = defaultdict(lambda: {"on_hand": D0, "reserved": D0, "free": D0})
    for sid, typ, wo_part_id, qty in db.execute(q):
        qty = Decimal(qty or 0)
        b = out[sid]
        b["on_hand"] += ON_HAND_SIGN.get(typ, 0) * qty
        if typ == "reserve":
            b["reserved"] += qty
        elif typ == "unreserve" or (typ == "issue" and wo_part_id):
            b["reserved"] -= qty
    for b in out.values():
        b["free"] = b["on_hand"] - b["reserved"]
    return out


def reserved_for_part(db: Session, wo_part_id) -> dict:
    """Сколько зарезервировано под позицию наряда по каждой ячейке (ещё не выдано)."""
    res = defaultdict(lambda: D0)
    for sid, typ, qty in db.execute(select(StockMovement.stock_item_id, StockMovement.type, StockMovement.qty)
                                    .where(StockMovement.wo_part_id == wo_part_id)):
        if typ == "reserve":
            res[sid] += qty
        elif typ in ("unreserve", "issue"):
            res[sid] -= qty
    return {k: v for k, v in res.items() if v > 0}


def catalog_free(db: Session, branch_id, catalog_ids) -> dict:
    """{catalog_id: свободно всего по всем ячейкам}"""
    items = db.scalars(select(StockItem).where(StockItem.catalog_id.in_(list(catalog_ids)))).all()
    bal = balances(db, branch_id, [i.id for i in items])
    out = defaultdict(lambda: D0)
    for i in items:
        out[i.catalog_id] += bal[i.id]["free"]
    return out


def search_catalog(db: Session, branch_id, q: str, limit: int = 12) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    rows = db.scalars(select(PartCatalog).where(PartCatalog.branch_id == branch_id, or_(
        PartCatalog.name.ilike(like), PartCatalog.article.ilike(like), PartCatalog.brand.ilike(like),
        PartCatalog.fits.ilike(like))).order_by(PartCatalog.name).limit(limit)).all()
    free = catalog_free(db, branch_id, [r.id for r in rows])
    last = {}
    for r in rows:
        last[r.id] = db.scalars(select(StockItem).where(StockItem.catalog_id == r.id).order_by(StockItem.updated_at.desc())).first()
    out = []
    for r in rows:
        si = last[r.id]
        used = r.kind == "used"
        out.append({"id": str(r.id), "name": r.name, "article": r.article, "brand": r.brand, "revision": r.revision,
                    "fits": r.fits, "free": float(free[r.id]), "used": used, "code": si.code if si else "",
                    # б/у: «закупка» = цена с разбора, наценка 0 — клиент платит цену разбора
                    "cost": float((si.sale_price if used else si.cost) if si else 0)})
    # Сначала то, что есть в наличии (б/у — как дешёвая альтернатива)
    out.sort(key=lambda x: (x["free"] <= 0, x["name"].lower()))
    return out


def get_or_create_catalog(db: Session, name: str, article: str = "", brand: str = "", revision: str = "") -> PartCatalog:
    name, article = name.strip()[:200], article.strip()[:80]
    q = select(PartCatalog).where(PartCatalog.name == name)
    q = q.where(PartCatalog.article == article) if article else q.where(PartCatalog.article == "")
    p = db.scalar(q)
    if p is None:
        from ..models.stock import guess_category, guess_section
        sec = guess_section(name)
        p = PartCatalog(name=name, article=article, brand=brand.strip()[:80], revision=revision.strip()[:40],
                        section=sec, category=guess_category(name, sec))
        db.add(p)
        db.flush()
    return p


def get_or_create_item(db: Session, catalog_id, location_id) -> StockItem:
    q = select(StockItem).where(StockItem.catalog_id == catalog_id)
    q = q.where(StockItem.location_id == location_id) if location_id else q.where(StockItem.location_id.is_(None))
    si = db.scalar(q)
    if si is None:
        si = StockItem(catalog_id=catalog_id, location_id=location_id)
        db.add(si)
        db.flush()
    return si


def move(db: Session, typ: str, item: StockItem, qty, wo_part: WoPart | None = None, purchase_id=None, comment=""):
    db.add(StockMovement(type=typ, stock_item_id=item.id, qty=Decimal(qty), purchase_id=purchase_id, comment=comment,
                         wo_part_id=wo_part.id if wo_part else None,
                         work_order_id=wo_part.work_order_id if wo_part else None))


# ---------- Подбор в наряд ----------
def reserve_from_stock(db: Session, branch_id, part: WoPart) -> bool:
    """Зарезервировать позицию со свободного остатка (можно из нескольких ячеек)."""
    items = db.scalars(select(StockItem).where(StockItem.catalog_id == part.catalog_id)).all()
    bal = balances(db, branch_id, [i.id for i in items])
    need = Decimal(part.qty)
    if sum((bal[i.id]["free"] for i in items), D0) < need:
        return False
    db.flush()
    for i in sorted(items, key=lambda i: -bal[i.id]["free"]):
        take = min(need, bal[i.id]["free"])
        if take > 0:
            move(db, "reserve", i, take, part, comment="Резерв со склада")
            need -= take
        if need <= 0:
            break
    part.status, part.source = "reserved", "from_stock"
    return True


def unreserve(db: Session, part: WoPart, comment="Снятие резерва"):
    for sid, qty in reserved_for_part(db, part.id).items():
        move(db, "unreserve", db.get(StockItem, sid), qty, part, comment=comment)


def draft_purchase(db: Session, branch_id) -> Purchase:
    """Текущая черновая закупка (одна на СТО): в неё падают все «Заказать»."""
    p = db.scalar(select(Purchase).where(Purchase.branch_id == branch_id, Purchase.status == "draft")
                  .order_by(Purchase.created_at))
    if p is None:
        p = Purchase(status="draft")
        db.add(p)
        db.flush()
    return p


def order_part(db: Session, branch_id, part: WoPart, qty_total=None) -> PurchaseLine:
    """Заказать у поставщика: строка закупки «под наряд». qty_total > нужного — излишек пойдёт на склад."""
    p = draft_purchase(db, branch_id)
    line = PurchaseLine(purchase_id=p.id, catalog_id=part.catalog_id, qty=Decimal(qty_total or part.qty),
                        price=part.cost, wo_part_id=part.id)
    if line.qty < part.qty:
        line.qty = part.qty
    db.add(line)
    part.status, part.source = "ordered", "purchased"
    return line


def refresh_order_status(db: Session, wo: WorkOrder):
    """Наряд ждёт запчасти → все пришли и в резерве → «Запчасти на складе»."""
    if wo.status != "waiting":
        return
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status != "returned")).all()
    if parts and all(p.status in ("reserved", "issued", "installed") for p in parts):
        osvc.set_status(db, wo, "instock", "Все запчасти пришли и в резерве")


def finish_picking(db: Session, wo: WorkOrder) -> str:
    """Запчастист закончил подбор. Возвращает ошибку или ''."""
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status != "returned")).all()
    if any(p.status == "need" for p in parts):
        return "По каждой запчасти выберите: со склада или заказать"
    if not parts:
        osvc.set_status(db, wo, "work", "Запчасти не нужны")
    elif all(p.status in ("reserved", "issued") for p in parts):
        osvc.set_status(db, wo, "instock", "Все запчасти в резерве на складе")
    else:
        osvc.set_status(db, wo, "waiting", "Ждём запчасти от поставщика")
    return ""


# ---------- Приход ----------
def receive_line(db: Session, branch_id, line: PurchaseLine, location_id):
    """Приход строки закупки: всё в ячейку, часть под наряд — сразу в резерв."""
    if line.received:
        return
    item = get_or_create_item(db, line.catalog_id, location_id)
    item.cost = line.price
    move(db, "in", item, line.qty, purchase_id=line.purchase_id, comment="Приход по закупке")
    line.received, line.location_id = True, location_id
    if line.wo_part_id:
        part = db.get(WoPart, line.wo_part_id)
        if part and part.status in ("ordered", "need"):
            move(db, "reserve", item, part.qty, part, comment="Пришло под наряд")
            part.status = "reserved"
            db.flush()
            refresh_order_status(db, db.get(WorkOrder, part.work_order_id))


def refresh_purchase_status(db: Session, p: Purchase):
    lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id)).all()
    if lines and all(l.received for l in lines):
        p.status = "arrived"
    elif any(l.received for l in lines):
        p.status = "partial"


# ---------- Выдача ----------
def request_issue(db: Session, wo: WorkOrder, comment: str = "") -> IssueRequest | None:
    """Запрос на выдачу всех зарезервированных и ещё не выданных позиций наряда."""
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "reserved")).all()
    if not parts:
        return None
    open_req = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status == "new"))
    if open_req:
        return open_req
    req = IssueRequest(work_order_id=wo.id, comment=comment.strip()[:500])
    db.add(req)
    db.flush()
    for p in parts:
        db.add(IssueRequestLine(request_id=req.id, wo_part_id=p.id, qty=p.qty))
    return req


def issue(db: Session, req: IssueRequest, user_id):
    """Кладовщик выдал: движения «выдача» из резерва, позиции «выдана», наряд → «В работе»."""
    for line in db.scalars(select(IssueRequestLine).where(IssueRequestLine.request_id == req.id)):
        part = db.get(WoPart, line.wo_part_id)
        if part.status != "reserved":
            continue
        for sid, qty in reserved_for_part(db, part.id).items():
            move(db, "issue", db.get(StockItem, sid), qty, part, comment="Выдано по запросу")
        line.issued_qty = line.qty
        part.status = "issued"
    req.status, req.issued_by = "issued", user_id
    db.flush()
    wo = db.get(WorkOrder, req.work_order_id)
    left = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.in_(("need", "ordered", "reserved")))).all()
    if wo.status == "instock" and not left:
        osvc.set_status(db, wo, "work", "Запчасти выданы слесарю")


def location_label(loc: Location | None) -> str:
    if not loc:
        return "без ячейки"
    return loc.label or " / ".join(x for x in (loc.rack, loc.shelf, loc.place) if x)


def backfill_categories(db: Session):
    """Разложить по разделам/категориям позиции, заведённые до появления категорий."""
    from ..models.stock import guess_category, guess_razbor_category, guess_section
    for c in db.scalars(select(PartCatalog).where((PartCatalog.section == "") | (PartCatalog.category == "")
                                                  | PartCatalog.section.is_(None) | PartCatalog.category.is_(None))):
        if c.kind == "used":
            if not c.category:
                c.category = guess_razbor_category(c.name)
            continue
        if not c.section:
            c.section = guess_section(c.name)
        if not c.category:
            c.category = guess_category(c.name, c.section)
