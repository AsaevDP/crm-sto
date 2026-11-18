"""Утверждение цен разбора (18.11.2026, брат).

Кладовщик разбора заводит деталь как раньше — она «Не утверждено» (StockItem.price_state="pending") и попадает
директору / заму / админу (право razbor.price) в «Цены разбора → Новые». Там: «Утвердить» (цену можно поправить) или
«Отказать» с причиной. Утверждено — деталь «В наличии». Отказано — «Отказано» (price_state="declined"): у кладовщика
вкладка «Отказано в утверждении» с причиной; отправить ещё раз можно сколько угодно раз — деталь висит в «Отказано»,
пока её не утвердят. Пока не утверждена — продать, зарезервировать, поставить в наряд, отправить, списать нельзя.

После утверждения кладовщик цену не меняет — только «Запросить изменение цены» (новая цена + причина) → у директора
вкладка «Изменение цены»; отказ — цена остаётся прежней.
"""
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import Donor, PartCatalog, StockItem
from ..models.razbor import RazborPrice

BLOCKED = ("pending", "declined")
NOT_BLOCKED = or_(StockItem.price_state.is_(None), StockItem.price_state == "")


def blocked(item: StockItem) -> bool:
    return (item.price_state or "") in BLOCKED


def block_error(item: StockItem) -> str:
    if (item.price_state or "") == "declined":
        return "Директор отказал в утверждении этой детали — с ней ничего делать нельзя, пока её не утвердят"
    if (item.price_state or "") == "pending":
        return "Цена детали ещё не утверждена директором — продать, зарезервировать или отправить её пока нельзя"
    return ""


def pending(db: Session, item_id, kind: str | None = None) -> RazborPrice | None:
    q = select(RazborPrice).where(RazborPrice.stock_item_id == item_id, RazborPrice.status == "pending")
    if kind:
        q = q.where(RazborPrice.kind == kind)
    return db.scalar(q.order_by(RazborPrice.created_at.desc()).limit(1))


def last_decline(db: Session, item_id) -> RazborPrice | None:
    return db.scalar(select(RazborPrice).where(RazborPrice.stock_item_id == item_id, RazborPrice.kind == "new",
                                               RazborPrice.status == "declined")
                     .order_by(RazborPrice.decided_at.desc()).limit(1))


def submit_new(db: Session, item: StockItem, note: str = "") -> RazborPrice:
    """Новая деталь (или повторно после отказа) — на утверждение. Отказанная остаётся «Отказано» до утверждения."""
    repeat = (item.price_state or "") == "declined"
    if not repeat:
        item.price_state = "pending"
    old = pending(db, item.id, "new")
    if old:   # уже ждёт — обновляем цену и пояснение
        old.price, old.note = item.sale_price, (note or old.note).strip()[:1000]
        return old
    r = RazborPrice(stock_item_id=item.id, kind="new", price=item.sale_price, note=note.strip()[:1000], repeat=repeat)
    db.add(r)
    db.flush()
    return r


def request_change(db: Session, item: StockItem, price: Decimal, note: str) -> str:
    if blocked(item):
        return "Деталь ещё не утверждена — цену поправит директор при утверждении"
    if price <= 0:
        return "Укажите новую цену"
    if not note.strip():
        return "Напишите причину — директору нужно понимать, почему меняем цену"
    if pending(db, item.id, "change"):
        return "Запрос на изменение цены уже отправлен — ждём директора"
    db.add(RazborPrice(stock_item_id=item.id, kind="change", price=price, old_price=item.sale_price,
                       note=note.strip()[:1000]))
    return ""


def approve(db: Session, r: RazborPrice, user, price: Decimal | None = None) -> str:
    item = db.get(StockItem, r.stock_item_id)
    if r.status != "pending" or not item:
        return "Этот запрос уже решён"
    p = price if price is not None and price > 0 else r.price
    if p <= 0:
        return "Цена должна быть больше нуля"
    item.sale_price = p
    if r.kind == "new":
        item.price_state = ""
    r.status, r.final_price, r.decided_by, r.decided_at = "approved", p, user.id, now()
    return ""


def decline(db: Session, r: RazborPrice, user, answer: str) -> str:
    if r.status != "pending":
        return "Этот запрос уже решён"
    if not answer.strip():
        return "Напишите причину отказа — кладовщик разбора увидит её у детали"
    item = db.get(StockItem, r.stock_item_id)
    if r.kind == "new" and item:
        item.price_state = "declined"
    r.status, r.answer, r.decided_by, r.decided_at = "declined", answer.strip()[:1000], user.id, now()
    return ""


# ---------- Цифры в меню ----------
def counts(db: Session, branch_id) -> dict:
    """Ждут директора: новые детали (🔴) и изменение цены (🟠); отказано и не отправлено снова (🔴 кладовщику разбора)."""
    base = select(func.count()).select_from(RazborPrice).where(RazborPrice.branch_id == branch_id,
                                                               RazborPrice.status == "pending")
    new = db.scalar(base.where(RazborPrice.kind == "new")) or 0
    change = db.scalar(base.where(RazborPrice.kind == "change")) or 0
    waiting = select(RazborPrice.stock_item_id).where(RazborPrice.status == "pending", RazborPrice.kind == "new")
    declined = db.scalar(select(func.count()).select_from(StockItem).where(
        StockItem.branch_id == branch_id, StockItem.price_state == "declined", StockItem.id.not_in(waiting))) or 0
    return {"new": new, "change": change, "declined": declined}


# ---------- Экран директора ----------
def by_donor(db: Session, branch_id) -> list[dict]:
    """Плитки по машинам: утверждено / не утверждено (ждёт), отказано."""
    items = db.scalars(select(StockItem).join(PartCatalog, PartCatalog.id == StockItem.catalog_id).where(
        StockItem.branch_id == branch_id, PartCatalog.kind == "used", StockItem.donor_id.is_not(None))).all()
    waiting = set(db.scalars(select(RazborPrice.stock_item_id).where(RazborPrice.branch_id == branch_id,
                                                                     RazborPrice.status == "pending",
                                                                     RazborPrice.kind == "new")))
    agg: dict = {}
    for i in items:
        a = agg.setdefault(i.donor_id, {"ok": 0, "wait": 0, "declined": 0})
        if i.id in waiting:
            a["wait"] += 1
        elif (i.price_state or "") == "declined":
            a["declined"] += 1
        else:
            a["ok"] += 1
    donors = {d.id: d for d in db.scalars(select(Donor).where(Donor.id.in_(list(agg))))} if agg else {}
    out = [{"d": donors[k], **v} for k, v in agg.items() if k in donors]
    out.sort(key=lambda x: (-x["wait"], -(x["d"].created_at.timestamp() if x["d"].created_at else 0)))
    return out
