"""«Всегда в наличии» (17.11.2026, брат): список запчастей и товаров, которые должны быть на складе всегда
(часто идут в ремонт) — с нормой «не меньше N». Видно, сколько есть, сколько уже едет, чего не хватает.
«Заказать недостающее» — черновик закупки на кладовщика (поставщика он выбирает сам), дальше обычный путь:
«Заказан» → приёмка с № накладной."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PartCatalog, Purchase, PurchaseLine, StockItem, StockKeep
from . import stock as svc

D0 = Decimal(0)


def rows(db: Session, branch_id) -> list[dict]:
    keeps = db.scalars(select(StockKeep).where(StockKeep.branch_id == branch_id)).all()
    if not keeps:
        return []
    cids = [k.catalog_id for k in keeps]
    items = db.scalars(select(StockItem).where(StockItem.catalog_id.in_(cids))).all()
    bal = svc.balances(db, branch_id, [i.id for i in items])
    on_hand, free = {c: D0 for c in cids}, {c: D0 for c in cids}
    for i in items:
        on_hand[i.catalog_id] += bal[i.id]["on_hand"]
        free[i.catalog_id] += bal[i.id]["free"]
    transit, draft = {c: D0 for c in cids}, {c: D0 for c in cids}
    for l, p in db.execute(select(PurchaseLine, Purchase).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                           .where(Purchase.branch_id == branch_id, PurchaseLine.catalog_id.in_(cids),
                                  PurchaseLine.received.is_(False), Purchase.status.in_(("draft", *svc.OPEN)))):
        # под наряд заказанное уйдёт в наряд — на склад «в запас» идёт только лишнее (без позиции наряда — всё)
        part_qty = D0
        if l.wo_part_id:
            from ..models import WoPart
            wp = db.get(WoPart, l.wo_part_id)
            part_qty = Decimal(wp.qty) if wp else D0
        extra = max(Decimal(l.left) - max(part_qty - l.got, D0), D0)
        (draft if p.status == "draft" else transit)[l.catalog_id] += extra
    out = []
    for k in keeps:
        c = db.get(PartCatalog, k.catalog_id)
        mn = Decimal(k.min_qty or 0)
        have = free[k.catalog_id]
        missing = max(mn - have - transit[k.catalog_id] - draft[k.catalog_id], D0)
        if have < mn and missing > 0:
            st = "red"        # ниже нормы и не заказано
        elif have < mn or have < mn * Decimal("1.2"):
            st = "yellow"     # ниже нормы, но уже едет / в черновике, или почти на норме
        else:
            st = "ok"
        out.append({"k": k, "c": c, "min": mn, "on_hand": on_hand[k.catalog_id], "free": have,
                    "transit": transit[k.catalog_id], "draft": draft[k.catalog_id], "missing": missing, "st": st,
                    "below": have < mn})
    order = {"red": 0, "yellow": 1, "ok": 2}
    out.sort(key=lambda r: (order[r["st"]], (r["c"].name if r["c"] else "").lower()))
    return out


def below_count(db: Session, branch_id) -> int:
    """Сколько позиций ниже нормы — сиреневая цифра в меню и на плитке склада."""
    return sum(1 for r in rows(db, branch_id) if r["below"])


def order_missing(db: Session, branch_id, user_id) -> Purchase | None:
    """«Заказать недостающее»: черновик закупки на этого сотрудника (на склад, без наряда), строки — сколько не хватает
    до нормы с учётом того, что уже едет. Поставщика подставляем, если у всех строк он один и тот же (брали там же)."""
    need = [r for r in rows(db, branch_id) if r["missing"] > 0]
    if not need:
        return None
    sups = {svc.last_supplier(db, r["c"].id) for r in need}
    sup = next(iter(sups)) if len(sups) == 1 else None
    p = Purchase(branch_id=branch_id, status="draft", supplier_id=sup, created_by=user_id,
                 note="«Всегда в наличии» — дозаказ до нормы")
    db.add(p)
    db.flush()
    for r in need:
        price = _last_price(db, r["c"].id)
        db.add(PurchaseLine(purchase_id=p.id, catalog_id=r["c"].id, qty=r["missing"], price=price, received_qty=0))
    db.flush()
    return p


def _last_price(db: Session, catalog_id) -> Decimal:
    pr = db.scalar(select(PurchaseLine.price).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                   .where(PurchaseLine.catalog_id == catalog_id, PurchaseLine.price > 0)
                   .order_by(PurchaseLine.created_at.desc()).limit(1))
    if pr:
        return Decimal(pr)
    cost = db.scalar(select(StockItem.cost).where(StockItem.catalog_id == catalog_id, StockItem.cost > 0).limit(1))
    return Decimal(cost or 0)
