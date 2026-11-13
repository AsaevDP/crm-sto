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
from ..models.orders import PART_OFF

D0 = Decimal("0")
ON_HAND_SIGN = {"in": 1, "return": 1, "issue": -1, "writeoff": -1, "sale": -1, "adjust": 1}
PART_STATUS_LABEL = {"need": "нужна", "ordered": "заказана", "arrived": "пришла", "reserved": "в резерве",
                     "issued": "выдана", "installed": "установлена", "returned": "возврат",
                     "option": "вариант", "declined": "не выбран", "archived": "в архиве"}


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
    from .stock_cat import by_barcode
    hit = by_barcode(db, branch_id, q)   # «пикнули» наклейку сканером — сразу эта деталь
    if hit:
        rows = [db.get(PartCatalog, hit.catalog_id)]
    else:
        # SQLite не различает регистр только у латиницы: «фильтр» и «Фильтр» ищем оба варианта
        conds = []
        for v in dict.fromkeys((q, q.lower(), q.capitalize(), q.upper())):
            like = f"%{v}%"
            conds += [PartCatalog.name.ilike(like), PartCatalog.article.ilike(like), PartCatalog.brand.ilike(like),
                      PartCatalog.fits.ilike(like)]
        rows = db.scalars(select(PartCatalog).where(PartCatalog.branch_id == branch_id, or_(*conds))
                          .order_by(PartCatalog.name).limit(limit)).all()
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
    if brand.strip() and not article:   # одна деталь разных фирм — разные позиции каталога (11.11.2026)
        from sqlalchemy import func as _f
        q = q.where(_f.lower(PartCatalog.brand) == brand.strip().lower())
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


def draft_owner(db: Session, work_order_id=None):
    """Чей черновик (13.11.2026): под наряд — всегда запчастиста этого наряда, кто бы ни нажал кнопку
    (менеджер выбрал вариант клиента — заказ всё равно у запчастиста в «Моих заказах»). На склад — того, кто заказывает."""
    from ..db import current_user_id
    if work_order_id:
        wo = db.get(WorkOrder, work_order_id)
        if wo and wo.partsman_id:
            return wo.partsman_id
    return current_user_id.get()


def _find_draft(db: Session, branch_id, supplier_id, work_order_id) -> Purchase | None:
    """Черновик владельца (см. draft_owner) у этого поставщика (None — без поставщика) под этот наряд (None — на склад)."""
    me = draft_owner(db, work_order_id)
    q = select(Purchase).where(Purchase.branch_id == branch_id, Purchase.status == "draft")
    q = q.where(Purchase.supplier_id == supplier_id) if supplier_id else q.where(Purchase.supplier_id.is_(None))
    q = q.where(Purchase.work_order_id == work_order_id) if work_order_id else q.where(Purchase.work_order_id.is_(None))
    if me:
        q = q.where(Purchase.created_by == me)
    return db.scalar(q.order_by(Purchase.created_at))


def draft_purchase(db: Session, branch_id, supplier_id=None, work_order_id=None) -> Purchase:
    """Черновик закупки. С 12.11.2026 у каждого запчастиста свои черновики (кто заказал = created_by):
    по поставщику и, если под наряд, — отдельный на каждый наряд (одна накладная — один наряд, проще с рекламациями).
    supplier_id не задан — черновик без поставщика (запчастист впишет)."""
    p = _find_draft(db, branch_id, supplier_id, work_order_id)
    if p is None:
        p = Purchase(status="draft", supplier_id=supplier_id, work_order_id=work_order_id,
                     created_by=draft_owner(db, work_order_id))
        db.add(p)
        db.flush()
    return p


def last_supplier(db: Session, catalog_id):
    """У кого брали эту запчасть в последний раз (для подсказки, в какой черновик класть)."""
    return db.scalar(select(Purchase.supplier_id).join(PurchaseLine, PurchaseLine.purchase_id == Purchase.id)
                     .where(PurchaseLine.catalog_id == catalog_id, Purchase.supplier_id.is_not(None),
                            Purchase.status != "draft").order_by(Purchase.created_at.desc()).limit(1))


def order_part(db: Session, branch_id, part: WoPart, qty_total=None) -> PurchaseLine:
    """Заказать у поставщика: строка закупки «под наряд». qty_total > нужного — излишек пойдёт на склад.
    Строка идёт в черновик этого запчастиста под этот наряд (12.11.2026). Поставщик подставляется тот,
    у кого брали эту деталь в прошлый раз (если есть такой черновик — туда же)."""
    sup = last_supplier(db, part.catalog_id)
    p = (_find_draft(db, branch_id, sup, part.work_order_id) if sup else None) \
        or _find_draft(db, branch_id, None, part.work_order_id) \
        or draft_purchase(db, branch_id, sup, part.work_order_id)
    line = PurchaseLine(purchase_id=p.id, catalog_id=part.catalog_id, qty=Decimal(qty_total or part.qty),
                        price=part.cost, wo_part_id=part.id)
    if line.qty < part.qty:
        line.qty = part.qty
    db.add(line)
    part.status, part.source = "ordered", "purchased"
    return line


def release_part(db: Session, part: WoPart, comment: str = "Позицию убрали из наряда"):
    """Снять резерв и убрать из черновой закупки (уже заказанное придёт на свободный остаток)."""
    if part.status in ("reserved", "ordered", "need"):   # «заказана» может быть частично в резерве (приход частями)
        unreserve(db, part, comment)
    for line in db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id == part.id)):
        if db.get(Purchase, line.purchase_id).status == "draft" and not line.ordered_at:
            db.delete(line)
        else:   # уже заказано у поставщика (вся закупка или эта строка) — приедет на свободный склад
            line.wo_part_id = None
    db.flush()


def issued_qty(db: Session, part: WoPart) -> dict:
    """Сколько этой позиции выдано слесарю и ещё не вернули — по ячейкам."""
    res = defaultdict(lambda: D0)
    for sid, typ, qty in db.execute(select(StockMovement.stock_item_id, StockMovement.type, StockMovement.qty)
                                    .where(StockMovement.wo_part_id == part.id, StockMovement.type.in_(("issue", "return")))):
        res[sid] += qty if typ == "issue" else -qty
    return {k: v for k, v in res.items() if v > 0}


def return_issued(db: Session, part: WoPart, comment: str = "Возврат на склад") -> Decimal:
    """Выданную слесарю запчасть вернули на склад (не понадобилась, наряд отменили). Возвращает, сколько вернули."""
    n = D0
    for sid, qty in issued_qty(db, part).items():
        move(db, "return", db.get(StockItem, sid), qty, part, comment=comment)
        n += qty
    return n


def has_movements(db: Session, part: WoPart) -> bool:
    return db.scalar(select(StockMovement.id).where(StockMovement.wo_part_id == part.id).limit(1)) is not None


def remove_part(db: Session, part: WoPart, comment: str = "Позицию убрали из наряда"):
    """Убрать позицию из наряда. Если по ней уже были движения склада (резерв, выдача) — строку не удаляем
    (на неё ссылается история склада, иначе ошибка сервера), а помечаем «возврат» — в счёт она не идёт."""
    release_part(db, part, comment)
    if part.status in ("issued", "installed"):
        return_issued(db, part, comment)
    for line in db.scalars(select(IssueRequestLine).where(IssueRequestLine.wo_part_id == part.id)):
        req = db.get(IssueRequest, line.request_id)
        if req and req.status in ("new", "ready"):
            db.delete(line)
    db.flush()
    from ..models import PayrollAccrual
    used = (has_movements(db, part)
            or db.scalar(select(IssueRequestLine.id).where(IssueRequestLine.wo_part_id == part.id).limit(1)) is not None
            or db.scalar(select(PayrollAccrual.id).where(PayrollAccrual.wo_part_id == part.id).limit(1)) is not None)
    if used:
        part.status, part.urgent = "returned", False
    else:
        db.delete(part)


def release_order(db: Session, wo: WorkOrder) -> int:
    """Наряд отменили: резервы снимаются, выданное возвращается на склад, черновые закупки чистятся,
    уже заказанное у поставщика придёт на свободный склад. Позиции снова «нужна» — если наряд откроют заново,
    запчастист подберёт их ещё раз. Возвращает, сколько позиций вернулось на склад."""
    n = 0
    for part in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id)).all():
        if part.status in ("reserved", "ordered", "need", "arrived"):
            if reserved_for_part(db, part.id):
                n += 1
            release_part(db, part, "Наряд отменён")
            part.status = "need"
        elif part.status in ("issued", "installed"):
            if return_issued(db, part, "Наряд отменён — возврат на склад") > 0:
                n += 1
            part.status = "need"
    for req in db.scalars(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status.in_(("new", "ready")))):
        req.status = "cancelled"
    db.flush()
    return n


def draft_part_ids(db: Session, part_ids) -> set:
    """Позиции «заказана», которые на самом деле лежат только в ЧЕРНОВИКЕ закупки (поставщику ещё не отправлено).
    12.11.2026: раньше они горели «заказана», а кладовщик их нигде не видел — теперь в наряде «в черновике закупки»."""
    ids = [i for i in part_ids if i]
    if not ids:
        return set()
    sent, draft = set(), set()
    for pid, st, oat in db.execute(select(PurchaseLine.wo_part_id, Purchase.status, PurchaseLine.ordered_at)
                                   .join(Purchase, Purchase.id == PurchaseLine.purchase_id).where(PurchaseLine.wo_part_id.in_(ids))):
        (draft if st == "draft" and not oat else sent).add(pid)   # 13.11.2026: «Заказал» по строке — уже заказана
    return draft - sent


def draft_waits(db: Session, branch_id, who=None) -> list[dict]:
    """Черновики закупок под наряды — запчастист ещё не нажал «Заказано». Для кладовщика («Приход») и запчастиста."""
    from ..models import User
    q = select(Purchase).where(Purchase.branch_id == branch_id, Purchase.status == "draft")
    if who:
        q = q.where(Purchase.created_by == who)
    out = []
    for p in db.scalars(q.order_by(Purchase.created_at)):
        lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id, PurchaseLine.wo_part_id.is_not(None))).all()
        if not lines:
            continue
        rows = []
        for l in lines:
            part = db.get(WoPart, l.wo_part_id)
            wo = db.get(WorkOrder, part.work_order_id) if part else None
            if not part or not wo or wo.status in ("closed", "cancel"):
                continue
            rows.append({"l": l, "c": db.get(PartCatalog, l.catalog_id), "part": part, "wo": wo})
        if rows:
            out.append({"p": p, "who": db.get(User, p.created_by) if p.created_by else None, "rows": rows,
                        "wo": rows[0]["wo"], "urgent": any(r["part"].urgent for r in rows)})
    return out


def unlink_line(db: Session, line: PurchaseLine):
    """Строку убрали из закупки: позиция наряда снова ждёт решения запчастиста («со склада» / «заказать»).
    Пришедшее частями снимается с резерва и становится свободным — «со склада» его же и возьмёт."""
    if not line.wo_part_id:
        return
    part = db.get(WoPart, line.wo_part_id)
    others = db.scalar(select(func.count()).select_from(PurchaseLine).where(
        PurchaseLine.wo_part_id == part.id, PurchaseLine.id != line.id)) if part else 0
    if part and part.status == "ordered" and not others:
        unreserve(db, part, "Строку убрали из закупки")
        part.status = "need"


PLAN_STATUSES = ("created", "diag", "approve")   # до согласования с клиентом — ничего не заказываем


def plan_part(db: Session, branch_id, part: WoPart, action: str = "auto", qty_total=None) -> str:
    """13.11.2026 (брат): подбор до согласования. Есть на складе — сразу в резерв (чтобы не забрали; клиент откажется —
    резерв снимется). Нет или «только заказать» — пометка «заказать после согласования», закупки ещё нет."""
    if action in ("auto", "stock") and reserve_from_stock(db, branch_id, part):
        part.plan = ""
        return "взято со склада в резерв"
    if action in ("auto", "order"):
        part.status, part.plan = "need", "order"
        part.plan_qty = Decimal(qty_total) if qty_total else None
        return "заказать — после согласования с клиентом"
    return "добавлено — выберите: со склада или заказать"


def apply_plan(db: Session, branch_id, wo: WorkOrder) -> list:
    """Менеджер согласовал с клиентом: помеченное «заказать» уходит запчастисту в черновик закупки (на его имя),
    нерешённое — со склада или в заказ. Возвращает позиции, которые надо заказать."""
    ordered = []
    for part in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "need")).all():
        if part.plan == "order":
            order_part(db, branch_id, part, part.plan_qty)
        else:
            source_part(db, branch_id, part, "auto")
        part.plan = ""
        if part.status == "ordered":
            ordered.append(part)
    db.flush()
    return ordered


def sent_lines(db: Session, part: WoPart) -> list:
    """Строки закупок, уже отправленные поставщику (не черновик), по этой позиции."""
    return [l for l in db.scalars(select(PurchaseLine).where(PurchaseLine.wo_part_id == part.id))
            if db.get(Purchase, l.purchase_id).status != "draft" or l.ordered_at]


def archive_part(db: Session, part: WoPart, user_id) -> str:
    """13.11.2026 (брат): «Убрать» → в архив наряда, а не насовсем. Резерв снимается, из черновика закупки строка уходит;
    уже заказанное у поставщика — запчастисту сообщение «отмените заказ» (приедет — ляжет на свободный склад).
    Возвращает ошибку или ''."""
    if part.status in ("issued", "installed"):
        return "Запчасть уже выдана мастеру — сначала «Вернуть на склад»"
    if part.status == "archived":
        return ""
    sent = sent_lines(db, part)
    release_part(db, part, "Убрали в архив наряда")
    for line in db.scalars(select(IssueRequestLine).where(IssueRequestLine.wo_part_id == part.id)):
        req = db.get(IssueRequest, line.request_id)
        if req and req.status in ("new", "ready"):
            db.delete(line)
    part.prev_status, part.status, part.urgent = part.status, "archived", False
    from ..db import now
    part.archived_at, part.archived_by = now(), user_id
    db.flush()
    if sent:
        from . import notify
        wo = db.get(WorkOrder, part.work_order_id)
        notify.send(db, [wo.partsman_id], "order_cancel", f"Отмените заказ: {part.name}",
                    f"Менеджер убрал позицию из наряда · {notify.car(db, wo)} · наряд {wo.number}. "
                    "Если всё же приедет — ляжет на свободный склад", f"/orders/{wo.id}#parts", wo, skip=user_id)
    return ""


def restore_part(db: Session, branch_id, wo: WorkOrder, part: WoPart) -> str:
    """Вернуть позицию из архива. Вариант на выбор — снова вариант (если клиент ещё выбирает). Обычная — снова «нужна»:
    до согласования с прежней пометкой, после — сразу со склада или в заказ запчастисту."""
    if part.status != "archived":
        return ""
    group_open = part.option_group and db.scalar(select(WoPart.id).where(
        WoPart.work_order_id == wo.id, WoPart.option_group == part.option_group, WoPart.status == "option").limit(1))
    if part.prev_status in ("option", "declined") and part.option_group:
        part.status = "option" if group_open else "declined"
    else:
        part.status = "need"
        if wo.status not in PLAN_STATUSES:
            source_part(db, branch_id, part, "order" if part.plan == "order" else "auto")
            part.plan = ""
        elif part.plan != "order":
            plan_part(db, branch_id, part, "auto")   # снова со склада, если есть, иначе «заказать после согласования»
    part.prev_status, part.archived_at, part.archived_by = "", None, None
    db.flush()
    return ""


def source_part(db: Session, branch_id, part: WoPart, action: str = "auto") -> str:
    """Позиция «нужна» → со склада (если есть свободный остаток) или в закупку. Возвращает итог для сообщения."""
    if action in ("auto", "stock") and reserve_from_stock(db, branch_id, part):
        return "взято со склада в резерв"
    if action in ("auto", "order"):
        order_part(db, branch_id, part)
        return "добавлено в закупку"
    return "добавлено — выберите: со склада или заказать"


def choose_option(db: Session, branch_id, wo: WorkOrder, part: WoPart) -> str:
    """Клиент выбрал вариант. Остальные варианты группы → «не выбран». Ошибка или ''."""
    group = [p for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id,
                                                       WoPart.option_group == part.option_group)) if p.id != part.id]
    if any(p.status in ("issued", "installed") for p in group):
        return "Другой вариант уже выдан слесарю — сначала верните его на склад"
    for p in group:
        if p.status not in ("option", "declined"):
            release_part(db, p, "Клиент выбрал другой вариант")
        p.status, p.source = "declined", ""
    if part.status in ("option", "declined"):
        part.status = "need"
        # 13.11.2026: до согласования — со склада в резерв или «заказать после согласования» (закупки ещё нет)
        if wo.status in PLAN_STATUSES and part.catalog_id:
            plan_part(db, branch_id, part, "auto")
        # Подбор уже идёт — сразу со склада или в закупку; до подбора позиция ждёт запчастиста
        elif wo.status in ("picking", "waiting", "instock", "work") and part.catalog_id:
            source_part(db, branch_id, part)
            if part.status == "ordered" and wo.status == "instock":
                osvc.set_status(db, wo, "waiting", f"Выбран вариант «{part.name}» — ждём от поставщика")
    return ""


def refresh_order_status(db: Session, wo: WorkOrder):
    """Наряд ждёт запчасти → все пришли и в резерве → «Запчасти на складе»."""
    if wo.status != "waiting":
        return
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.notin_(PART_OFF))).all()
    if parts and all(p.status in ("reserved", "issued", "installed") for p in parts):
        osvc.set_status(db, wo, "instock", "Все запчасти пришли и в резерве")
        from . import notify
        notify.parts_ready(db, wo)   # одно сообщение, когда пришло ВСЁ (11.11.2026)


def finish_picking(db: Session, wo: WorkOrder) -> str:
    """Запчастист закончил подбор. Возвращает ошибку или ''."""
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.notin_(PART_OFF))).all()
    waiting_choice = db.scalar(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "option"))
    if waiting_choice:
        return f"Клиент ещё не выбрал вариант: «{waiting_choice.name}»"
    if any(p.status == "need" for p in parts):
        return "По каждой запчасти выберите: со склада или заказать"
    if not parts:
        osvc.set_status(db, wo, "work", "Запчасти не нужны")
    elif all(p.status in ("issued", "installed") for p in parts):
        # 12.11.2026 (живой прогон): новая работа во время ремонта без новых запчастей — всё уже выдано, сразу в работу
        osvc.set_status(db, wo, "work", "Новых запчастей не нужно — всё уже выдано")
    elif all(p.status in ("reserved", "issued") for p in parts):
        osvc.set_status(db, wo, "instock", "Все запчасти в резерве на складе")
        from . import notify
        notify.parts_ready(db, wo)
    else:
        osvc.set_status(db, wo, "waiting", "Ждём запчасти от поставщика")
    return ""


def mark_picked(db: Session, wo: WorkOrder, user) -> str:
    """13.11.2026 (брат): запчастист закончил подбор, пока менеджер согласует — «Подбор завершён» ничего не заказывает,
    только отмечает и пишет менеджеру: можно звонить клиенту. Возвращает ошибку или ''."""
    from ..db import now
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "need")).all()
    undecided = [p for p in parts if p.plan != "order" and not p.option_group]
    if undecided:
        return f"По каждой запчасти выберите: со склада или заказать («{undecided[0].name}»)"
    wo.picked_at, wo.picked_by = now(), user.id
    from . import notify
    n = db.scalar(select(func.count()).select_from(WoPart).where(WoPart.work_order_id == wo.id,
                                                                 WoPart.status.notin_(("returned", "declined", "archived"))))
    notify.send(db, [wo.manager_id], "picked", f"Подбор готов — согласуйте с клиентом · {wo.number}",
                f"{notify.car(db, wo)} · запчастей: {n or 0} · подобрал {user.short_name}", f"/orders/{wo.id}#parts", wo,
                skip=user.id)
    return ""


def after_approval(db: Session, wo: WorkOrder) -> str:
    """13.11.2026 (брат): менеджер согласовал с клиентом запчасти и работы — только теперь заказываем. Со склада —
    в резерве; «заказать» — в черновик запчастиста (на его имя) + сообщение «Заказать запчасти». Дальше — как раньше:
    всё есть → «Запчасти на складе», что-то едет → «Ждёт запчасти», запчастей нет → «В работе»."""
    waiting_choice = db.scalar(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "option"))
    if waiting_choice:
        return f"Клиент ещё не выбрал вариант: «{waiting_choice.name}» — нажмите «Выбрать» у нужного или уберите в архив"
    ordered = apply_plan(db, wo.branch_id, wo)
    err = finish_picking(db, wo)
    if err:
        return err
    if ordered:
        from . import notify
        names = ", ".join(p.name for p in ordered[:4]) + ("…" if len(ordered) > 4 else "")
        notify.send(db, [wo.partsman_id], "to_order", f"Заказать запчасти: {len(ordered)} поз. · {wo.number}",
                    f"{notify.car(db, wo)} · {names}", "/my-orders?st=draft", wo)
    return ""


# ---------- Приход ----------
def default_location(db: Session, catalog_id):
    """Куда класть пришедшее: в ячейку, где эта запчасть уже лежит (больше всего штук), иначе — никуда."""
    items = db.scalars(select(StockItem).where(StockItem.catalog_id == catalog_id, StockItem.location_id.is_not(None))).all()
    if not items:
        return None
    bal = balances(db, items[0].branch_id, [i.id for i in items])
    return max(items, key=lambda i: bal[i.id]["on_hand"]).location_id


def receive_line(db: Session, branch_id, line: PurchaseLine, location_id, qty=None) -> Decimal:
    """Приход строки закупки (целиком или частью qty): в ячейку, под наряд — сразу в резерв.
    Ячейка не выбрана — кладём туда, где эта запчасть уже лежит. Возвращает, сколько принято."""
    left = line.left
    if line.received or left <= 0:
        return D0
    take = left if qty is None else min(Decimal(qty), left)
    if take <= 0:
        return D0
    if location_id is None:
        location_id = default_location(db, line.catalog_id)
    item = get_or_create_item(db, line.catalog_id, location_id)
    item.cost = line.price
    move(db, "in", item, take, purchase_id=line.purchase_id, comment="Приход по закупке")
    line.received_qty = line.got + take
    line.received, line.location_id = line.got >= line.qty, location_id
    if line.wo_part_id:
        part = db.get(WoPart, line.wo_part_id)
        owner = db.get(WorkOrder, part.work_order_id) if part else None
        if owner and owner.status in ("cancel", "closed"):   # наряд отменён — пришедшее идёт на свободный склад
            part = None
        if part and part.status in ("ordered", "need"):
            db.flush()
            have = sum(reserved_for_part(db, part.id).values(), D0)
            put = min(take, Decimal(part.qty) - have)
            if put > 0:
                move(db, "reserve", item, put, part, comment="Пришло под наряд" + ("" if have + put >= part.qty else " (часть)"))
            if have + max(put, D0) >= part.qty:
                part.status = "reserved"
                db.flush()
                wo = db.get(WorkOrder, part.work_order_id)
                refresh_order_status(db, wo)   # пришло всё → «Все запчасти на складе»
                # Пришло, но не всё — одно сообщение на всю приёмку пишет arrived_notify (12.11.2026)
    return take


def arrived_notify(db: Session, parts: list, before: dict):
    """После приёмки: по каждому наряду, где запчасти пришли, но не всё, — «Пришли запчасти: N» менеджеру,
    запчастисту и всем мастерам. Наряд, который этой приёмкой стал «Запчасти на складе», уже получил своё."""
    from . import notify
    by_wo: dict = {}
    for p in parts:
        if p.status == "reserved":
            by_wo.setdefault(p.work_order_id, []).append(p)
    for wid, lst in by_wo.items():
        wo = db.get(WorkOrder, wid)
        if not wo or wo.status in ("cancel", "closed"):
            continue
        if wo.status == "instock" and before.get(wid) != "instock":
            continue
        notify.parts_arrived(db, wo, lst)


# Закупка ждёт приёмки: заказана / привезли (кладовщик отметил, ещё не принял) / неполная поставка (ждём остальное)
OPEN = ("ordered", "delivered", "partial")


def refresh_purchase_status(db: Session, p: Purchase):
    lines = db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id)).all()
    if p.status == "draft":
        return
    if lines and all(l.received for l in lines):
        p.status = "arrived"
    elif any(l.got > 0 for l in lines):
        p.status = "partial"
    elif p.status != "delivered":
        p.status = "ordered"


def mark_delivered(db: Session, p: Purchase, user, invoice: str = "") -> bool:
    """Кладовщик: «Привезли» — товар на СТО, ждёт приёмки. Запчастист видит это в «Моих заказах»."""
    from ..db import now
    if p.status not in ("ordered", "partial"):
        return False
    p.status, p.delivered_at, p.delivered_by = "delivered", now(), user.id
    if invoice.strip():
        p.supplier_invoice = invoice.strip()[:80]
    return True


def move_rest(db: Session, branch_id, line: PurchaseLine, target: Purchase | None) -> PurchaseLine | None:
    """Поставщик привёз не всё: остаток строки — в другую закупку (черновик), строка закрывается тем, что пришло.
    Под наряд — новая строка тоже под этот наряд; резерв досчитается при её приходе."""
    rest = line.left
    if rest <= 0:
        return None
    target = target or draft_purchase(db, branch_id, None, db.get(Purchase, line.purchase_id).work_order_id)
    new = PurchaseLine(purchase_id=target.id, catalog_id=line.catalog_id, qty=rest, price=line.price,
                       wo_part_id=line.wo_part_id, received_qty=0)
    db.add(new)
    line.qty = line.got
    line.received = True
    if line.qty <= 0:   # ничего не пришло — строку просто переносим
        db.delete(line)
    db.flush()
    return new


def delete_purchase(db: Session, p: Purchase):
    """Удалить черновик закупки: позиции нарядов из него снова «нужна»."""
    for line in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == p.id)).all():
        unlink_line(db, line)
        db.delete(line)
    db.flush()
    db.delete(p)


# ---------- Вставка списка (из Excel / счёта поставщика) ----------
def _num(s: str):
    s = (s or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    for suf in ("шт", "грн", "₴", "uah"):
        if s.lower().endswith(suf):
            s = s[: -len(suf)]
    try:
        return Decimal(s) if s else None
    except Exception:
        return None


def _looks_article(s: str) -> bool:
    s = s.strip()
    return bool(s) and " " not in s and len(s) <= 30 and any(ch.isdigit() for ch in s)


def parse_paste(text: str) -> list[dict]:
    """Строки «Название; Артикул; Бренд; Кол-во; Цена» — через Tab (из Excel), «;» или «|».
    Порядок текстовых колонок угадываем: похожее на артикул (без пробелов, с цифрами) — артикул.
    Числа в конце строки — количество и цена. Шапку таблицы и пустые строки пропускаем."""
    out = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        sep = "\t" if "\t" in raw else (";" if ";" in raw else ("|" if "|" in raw else None))
        cells = [c.strip() for c in (raw.split(sep) if sep else [raw])]
        cells = [c for c in cells if c != ""]
        nums = []
        while cells and _num(cells[-1]) is not None and len(nums) < 2:
            nums.insert(0, _num(cells.pop()))
        if not cells:
            continue
        low = " ".join(cells).lower()
        if not nums and len(cells) > 1 and any(w in low for w in ("наименование", "название", "артикул")):
            continue   # шапка
        name, article, brand = "", "", ""
        texts = list(cells)
        arts = [c for c in texts if _looks_article(c)]
        if arts and len(texts) > 1:
            article = arts[0]
            texts.remove(article)
        name = max(texts, key=len) if texts else article
        texts = [t for t in texts if t != name]
        brand = texts[0] if texts else ""
        qty = nums[0] if nums else Decimal(1)
        price = nums[1] if len(nums) > 1 else Decimal(0)
        if len(nums) == 1 and nums[0] != nums[0].to_integral_value():   # одно дробное число — это цена
            qty, price = Decimal(1), nums[0]
        out.append({"name": name[:200], "article": article[:80], "brand": brand[:80],
                    "qty": qty if qty and qty > 0 else Decimal(1), "price": money(price or 0)})
    return out


# ---------- Выдача ----------
def request_issue(db: Session, wo: WorkOrder, comment: str = "") -> IssueRequest | None:
    """Запрос на выдачу всех зарезервированных и ещё не выданных позиций наряда.
    Позиции, которые уже в открытом запросе (ждёт кладовщика или «готово к выдаче»), второй раз не просим."""
    open_ids = [r.id for r in db.scalars(select(IssueRequest).where(IssueRequest.work_order_id == wo.id,
                                                                    IssueRequest.status.in_(("new", "ready"))))]
    asked = set(db.scalars(select(IssueRequestLine.wo_part_id).where(IssueRequestLine.request_id.in_(open_ids)))) if open_ids else set()
    parts = [p for p in db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status == "reserved"))
             if p.id not in asked]
    open_req = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status == "new"))
    if open_req:
        for p in parts:   # дозаказали ещё деталь — добавляем в тот же запрос
            db.add(IssueRequestLine(request_id=open_req.id, wo_part_id=p.id, qty=p.qty))
        return open_req
    if not parts:
        return None
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
    from ..db import now
    req.status, req.issued_by, req.issued_at = "issued", user_id, now()
    # «Готово к выдаче» по этому запросу больше не горит
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.work_order_id == req.work_order_id,
                                                   Notification.kind == "issue_ready", Notification.read_at.is_(None))):
        n.read_at = now()
    db.flush()
    wo = db.get(WorkOrder, req.work_order_id)
    left = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.in_(("need", "ordered", "reserved")))).all()
    if wo.status == "instock" and not left:
        osvc.set_status(db, wo, "work", "Запчасти выданы слесарю")


def mark_ready(db: Session, req: IssueRequest, user) -> bool:
    """Кладовщик собрал запчасти у окошка: «Готово к выдаче». Менеджеру наряда и тому, кто просил, — сообщение."""
    from ..db import now
    from . import notify as nsvc
    if req.status != "new":
        return False
    req.status, req.ready_at, req.ready_by = "ready", now(), user.id
    wo = db.get(WorkOrder, req.work_order_id)
    nsvc.send(db, [wo.manager_id, req.created_by], "issue_ready", "Запчасти готовы к выдаче — у кладовщика",
              f"{nsvc.car(db, wo)} · наряд {wo.number}", "/ready", wo, skip=user.id)
    return True


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


# ======================= Заказы запчастистов (12.11.2026) =======================
# Кто заказал = created_by закупки (или строки, если добавил в чужую). Пришло — по приходу на склад (received_qty).
ORDER_STATES = {"draft": ("Черновики", "gray", "ещё не отправлено"), "wait": ("Заказано", "orange", "ждём от поставщика"),
                "delivered": ("Привезли — ждут приёмки", "teal", "кладовщик проверяет"),
                "partial": ("Неполная поставка", "purple", "ждём остальное"), "late": ("Опаздывают", "red", "срок прошёл"),
                "arrived": ("Приняты на склад", "green", "за 60 дней")}


def partsman_lines(db: Session, branch_id, uid=None, days: int = 60) -> list[dict]:
    """Строки закупок запчастиста (uid=None — всех): деталь, наряд, сколько нужно под наряд и сколько на склад,
    поставщик, сколько пришло, статус. Пришедшее — за последние `days` дней."""
    from datetime import date, datetime, timedelta, timezone
    from ..models import Supplier, User, Vehicle, WorkOrder
    q = (select(PurchaseLine, Purchase).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
         .where(Purchase.branch_id == branch_id))
    if uid:
        q = q.where(or_(Purchase.created_by == uid, PurchaseLine.created_by == uid))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    today = date.today()
    raw = []
    for l, p in db.execute(q.order_by(Purchase.created_at.desc(), PurchaseLine.created_at)):
        if p.status == "draft":
            st = "draft"
        elif l.received:
            st = "arrived"
            when = l.updated_at or p.updated_at
            if when and when.replace(tzinfo=when.tzinfo or timezone.utc) < since:
                continue
        elif p.status == "delivered":
            st = "delivered"          # привезли — кладовщик пересчитывает и принимает
        elif l.got > 0:
            st = "partial"
        else:
            st = "wait"
        late = st in ("wait", "partial") and p.expected_on is not None and p.expected_on < today
        raw.append((l, p, st, late))
    parts = {x.id: x for x in db.scalars(select(WoPart).where(WoPart.id.in_([l.wo_part_id for l, *_ in raw if l.wo_part_id])))}
    wo_ids = {x.work_order_id for x in parts.values()} | {p.work_order_id for _, p, *_ in raw if p.work_order_id}
    orders = {o.id: o for o in db.scalars(select(WorkOrder).where(WorkOrder.id.in_(list(wo_ids))))}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders.values()])))}
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([l.catalog_id for l, *_ in raw])))}
    sups = {s.id: s for s in db.scalars(select(Supplier).where(Supplier.branch_id == branch_id))}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == branch_id))}
    out = []
    for l, p, st, late in raw:
        part = parts.get(l.wo_part_id)
        wo = orders.get(part.work_order_id) if part else None
        need = Decimal(part.qty) if part else Decimal(0)
        out.append({"l": l, "p": p, "st": st, "late": late, "c": cats.get(l.catalog_id), "part": part, "wo": wo,
                    "v": vehicles.get(wo.vehicle_id) if wo else None, "sup": sups.get(p.supplier_id),
                    "buyer": people.get(l.created_by if l.created_by and l.created_by != p.created_by else p.created_by),
                    "need": need, "extra": max(Decimal(l.qty) - need, Decimal(0)) if part else Decimal(l.qty)})
    return out


def order_counts(rows: list[dict]) -> dict:
    c = {k: 0 for k in ORDER_STATES}
    for r in rows:
        c[r["st"]] += 1
        if r["late"]:
            c["late"] += 1
    return c
