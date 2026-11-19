"""Химия для мойки и детейлинга (12.11.2026, решение брата).

- На складе — третий раздел «Химия для ухода за авто» (section="chem"), своя полка. Приходит через обычные закупки.
- Мойщик / детейлер просит химию у кладовщика (ChemRequest) → у кладовщика окошко «Выдать» / «Отказать».
- «Выдал» = движение «выдача» со склада без позиции наряда: остаток уменьшается, это расход СТО (входит в цену мойки),
  в счёт клиенту не идёт. Кто сколько взял — в «Мойке» и в журнале.
- Заказывают химию менеджер, директор, зам («Мойка → Закупка для мойки», право wash.buy).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import ChemRequest, PartCatalog, StockItem, StockMovement, User, WorkOrder
from . import stock as ssvc
from . import notify as nsvc

D0 = Decimal(0)
STATUS = {"new": "ждёт кладовщика", "ready": "готово — у окошка кладовщика", "issued": "выдано", "declined": "отказ",
          "cancelled": "отменён"}
OPEN = ("new", "ready")


def _stock(db: Session, branch_id, catalog_id):
    stock = db.scalars(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.catalog_id == catalog_id)
                       .order_by(StockItem.created_at)).all()
    return stock, ssvc.balances(db, branch_id, [i.id for i in stock])


def _reserved(db: Session, r: ChemRequest) -> dict:
    """Сколько химии отложено под этот запрос по каждой ячейке (19.11.2026: «Готово к выдаче»)."""
    out = {}
    for sid, typ, qty in db.execute(select(StockMovement.stock_item_id, StockMovement.type, StockMovement.qty)
                                    .where(StockMovement.chem_request_id == r.id)):
        out[sid] = out.get(sid, D0) + (Decimal(qty) if typ == "reserve" else -Decimal(qty) if typ == "unreserve" else D0)
    return {k: v for k, v in out.items() if v > 0}


def unreserve(db: Session, r: ChemRequest):
    """Снять отложенное под запрос (выдали, отказали, отменили)."""
    for sid, qty in _reserved(db, r).items():
        db.add(StockMovement(type="unreserve", stock_item_id=sid, qty=qty, chem_request_id=r.id,
                             comment="Химия: снят резерв под запрос"))
    db.flush()


def mark_ready(db: Session, r: ChemRequest, user: User, qty: Decimal | None = None) -> str:
    """«📦 Готово к выдаче» (19.11.2026, брат): кладовщик поставил химию у окошка — она откладывается (резерв), чтобы
    её не отдали другому. Не хватает на складе — нельзя (подготовьте меньше или откажите). Тому, кто просил, —
    сообщение и зелёная цифра у «Моих запросов». Ошибка или ''."""
    if r.status != "new":
        return "Запрос уже закрыт"
    qty = Decimal(qty if qty is not None else r.qty)
    if qty <= 0:
        return "Сколько подготовили?"
    stock, bal = _stock(db, r.branch_id, r.catalog_id)
    free = sum((bal[i.id]["free"] for i in stock if i.id in bal), D0)
    if free < qty:
        return (f"На складе свободно только {free.normalize():f} — подготовьте столько или откажите"
                if free > 0 else "На складе этой химии нет — откажите с причиной (например, «закончилась — заказали»)")
    left = qty
    for i in stock:
        take = min(bal[i.id]["free"] if i.id in bal else D0, left)
        if take <= 0:
            continue
        db.add(StockMovement(type="reserve", stock_item_id=i.id, qty=take, chem_request_id=r.id,
                             work_order_id=r.work_order_id, comment="Химия: готово к выдаче"))
        left -= take
        if left <= 0:
            break
    r.status, r.issued_qty, r.ready_at, r.ready_by = "ready", qty, now(), user.id
    c = db.get(PartCatalog, r.catalog_id)
    if r.created_by:
        nsvc.send(db, [r.created_by], "chem_ready", f"Химия готова к выдаче: {c.name if c else ''}",
                  f"{qty.normalize():f} шт · подойдите к кладовщику", "/my-requests?tab=chem", skip=user.id)
    return ""


def items(db: Session, branch_id) -> list[dict]:
    """Вся химия на складе: позиция справочника, свободно всего, ячейка (первая), закупочная цена."""
    cats = db.scalars(select(PartCatalog).where(PartCatalog.section == "chem", PartCatalog.kind != "used")
                      .order_by(PartCatalog.category, PartCatalog.name)).all()
    if not cats:
        return []
    stock = db.scalars(select(StockItem).where(StockItem.branch_id == branch_id,
                                               StockItem.catalog_id.in_([c.id for c in cats]))).all()
    bal = ssvc.balances(db, branch_id, [i.id for i in stock])
    out = {c.id: {"c": c, "free": D0, "loc": None, "cost": D0} for c in cats}
    for i in stock:
        r = out[i.catalog_id]
        r["free"] += bal[i.id]["free"] if i.id in bal else D0
        if r["loc"] is None and i.location_id:
            r["loc"] = i.location_id
        r["cost"] = r["cost"] or (i.cost or D0)
    return list(out.values())


def ask(db: Session, user: User, catalog_id, qty: Decimal, work_order_id=None, note: str = "") -> ChemRequest:
    from .messages import storekeepers
    c = db.get(PartCatalog, catalog_id)
    r = ChemRequest(catalog_id=catalog_id, qty=qty, work_order_id=work_order_id, note=(note or "").strip()[:300])
    db.add(r)
    db.flush()
    wo = db.get(WorkOrder, work_order_id) if work_order_id else None
    nsvc.send(db, storekeepers(db, user.branch_id), "chem_ask", f"Химия: {user.short_name} просит {c.name if c else ''}",
              f"{qty.normalize():f} шт" + (f" · {nsvc.car(db, wo)}" if wo else "") + (f" · {r.note}" if r.note else ""),
              "/stock/issues?tab=chem", wo, skip=user.id)
    return r


def issue(db: Session, r: ChemRequest, user: User, qty: Decimal | None = None) -> str:
    """Выдать со склада — сразу или после «Готово к выдаче» (тогда отложенное снимается с резерва и выдаётся).
    Берём из ячеек, где есть свободное. Возвращает текст ошибки или ""."""
    if r.status not in OPEN:
        return "Запрос уже закрыт"
    if r.status == "ready" and qty is None:
        qty = r.issued_qty
    qty = Decimal(qty if qty is not None else r.qty)
    if qty <= 0:
        return "Сколько выдаёте?"
    if r.status == "ready":
        unreserve(db, r)
    stock, bal = _stock(db, user.branch_id, r.catalog_id)
    free = sum((bal[i.id]["free"] for i in stock if i.id in bal), D0)
    if free < qty:
        return f"На складе свободно только {free.normalize():f} — выдать {qty.normalize():f} нельзя"
    who = db.get(User, r.created_by) if r.created_by else None
    left = qty
    for i in stock:
        f = bal[i.id]["free"] if i.id in bal else D0
        take = min(f, left)
        if take <= 0:
            continue
        db.add(StockMovement(type="issue", stock_item_id=i.id, qty=take, work_order_id=r.work_order_id,
                             comment=f"Химия для мойки — {who.full_name if who else ''}".strip(" —")))
        left -= take
        if left <= 0:
            break
    was_ready = r.status == "ready"
    r.status, r.issued_qty, r.decided_at, r.decided_by = "issued", qty, now(), user.id
    c = db.get(PartCatalog, r.catalog_id)
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.user_id == r.created_by, Notification.kind == "chem_ready",
                                                   Notification.read_at.is_(None),
                                                   Notification.title.endswith(c.name if c else ""))):
        n.read_at = now()   # выдали — «готово, подойдите» больше не нужно
    if who and not was_ready:   # выдали сразу (человек стоял рядом) — сообщение для истории
        nsvc.send(db, [who.id], "chem_ok", f"Химия выдана: {c.name if c else ''}", f"{qty.normalize():f} шт",
                  "/my-requests?tab=chem", skip=user.id)
    return ""


def cancel(db: Session, r: ChemRequest):
    """Мойщик отменил запрос — отложенное (если кладовщик уже подготовил) возвращается в свободное."""
    if r.status == "ready":
        unreserve(db, r)
    r.status = "cancelled"


def decline(db: Session, r: ChemRequest, user: User, reason: str):
    if r.status == "ready":
        unreserve(db, r)
    r.status, r.decided_at, r.decided_by, r.reason = "declined", now(), user.id, (reason or "").strip()[:200]
    c = db.get(PartCatalog, r.catalog_id)
    if r.created_by:
        nsvc.send(db, [r.created_by], "chem_no", f"Химию не выдали: {c.name if c else ''}", r.reason or "Без причины", "/wash/my",
                  skip=user.id)


def requests(db: Session, branch_id, status=None, who=None, days: int = 30) -> list[dict]:
    q = select(ChemRequest).where(ChemRequest.branch_id == branch_id)
    if status:
        q = q.where(ChemRequest.status.in_(status if isinstance(status, (list, tuple)) else [status]))
    if who:
        q = q.where(ChemRequest.created_by == who)
    if days:
        q = q.where(ChemRequest.created_at >= datetime.now(timezone.utc) - timedelta(days=days))
    rows = db.scalars(q.order_by(ChemRequest.created_at.desc()).limit(300)).all()
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([r.catalog_id for r in rows])))}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == branch_id))}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([r.work_order_id for r in rows if r.work_order_id])))}
    return [{"r": r, "c": cats.get(r.catalog_id), "who": people.get(r.created_by), "by": people.get(r.decided_by),
             "wo": orders.get(r.work_order_id)} for r in rows]


def usage_by_person(db: Session, branch_id, days: int = 30) -> list[dict]:
    """Кто сколько химии взял за период (выдано): человек → позиций, штук, на сумму по закупке."""
    rows = requests(db, branch_id, "issued", days=days)
    cost = {x["c"].id: x["cost"] for x in items(db, branch_id)}
    agg = {}
    for x in rows:
        if not x["who"]:
            continue
        a = agg.setdefault(x["who"].id, {"u": x["who"], "n": 0, "qty": D0, "sum": D0})
        a["n"] += 1
        a["qty"] += x["r"].issued_qty or D0
        a["sum"] += (x["r"].issued_qty or D0) * cost.get(x["r"].catalog_id, D0)
    return sorted(agg.values(), key=lambda a: -a["sum"])
