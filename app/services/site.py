"""«На сайт» (22.11.2026, брат) — что продаём через сайт: две вкладки.

Б/у · разбор — детали разбора с галочкой «на сайт», утверждённой ценой и в наличии (как раньше плитка «На сайте»).

Новые — новые запчасти со склада на продажу (SiteNew):
  • от которых отказался клиент: запчастист в «Лишнем после отказов» нажал «Продать» → сюда сама (sync_leftovers);
  • выставленные из карточки склада кнопкой «На продажу» (кладовщик, главный кладовщик, директор, зам, админ).
Цена — та, что была для клиента (запчастист с наценкой); из карточки склада — закупка + стандартная наценка.
Кладовщик разбора цену не меняет. На сайт — только после утверждения директором / замом / админом (razbor.price).
Деталь остаётся обычной позицией склада: её можно взять в наряд — тогда свободного остатка нет и с сайта она уходит
сама («нет на складе»). Покупатель взял в резерв — в наряд уже не дать (резерв склада). Продажа — как у б/у:
UsedSale (site_new_id) → «Продажи и отправки», «Выдача и отправка», «продано».
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PartCatalog, PartLeftover, StockItem, UsedSale, WoPart, WorkOrder
from ..models.razbor import SiteNew, SiteNewPhoto
from . import settings as st
from . import stock as ssvc

D0 = Decimal(0)
LIVE_SALES = ("reserved", "sent", "sold")
STAGES = {"wait": ("Ждут утверждения", "orange"), "site": ("На сайте", "green"), "nostock": ("Нет на складе", "gray"),
          "declined": ("Отказано", "red"), "sold": ("Продано", "teal"), "off": ("Сняты с продажи", "gray")}


def next_code(db: Session) -> str:
    codes = [c for c in db.scalars(select(SiteNew.code).where(SiteNew.code.like("Н-%"))) if c]
    n = max((int(c.split("-")[1]) for c in codes if c.split("-")[1].isdigit()), default=0) + 1
    return f"Н-{n:04d}"


# ---------- Откуда берутся ----------
def sync_leftovers(db: Session, branch_id) -> int:
    """«Лишнее после отказов» со статусом «на продажу» → новая деталь на сайт (один раз на отказ). Запчастист потом
    передумал («Оставить на складе», «Вернуть поставщику») — снимаем с продажи. Возвращает, сколько добавлено."""
    have = {s.leftover_id: s for s in db.scalars(select(SiteNew).where(SiteNew.branch_id == branch_id,
                                                                       SiteNew.leftover_id.is_not(None)))}
    added = 0
    for lo in db.scalars(select(PartLeftover).where(PartLeftover.branch_id == branch_id)):
        sn = have.get(lo.id)
        if lo.status == "sale" and sn is None and lo.catalog_id:
            part = db.get(WoPart, lo.wo_part_id)
            price = part.price if part and part.price else D0
            db.add(SiteNew(catalog_id=lo.catalog_id, code=next_code(db), qty=lo.qty, price=price, source="refused",
                           leftover_id=lo.id, work_order_id=lo.work_order_id, reason=lo.reason or "",
                           branch_id=branch_id))
            db.flush()
            added += 1
        elif sn and lo.status != "sale" and sn.status == "on":
            sn.status = "off"
        elif sn and lo.status == "sale" and sn.status == "off" and not sold_qty(db, [sn.id]).get(sn.id):
            sn.status = "on"   # снова «Продать»
    return added


def offer_from_stock(db: Session, item: StockItem, qty: Decimal) -> SiteNew | str:
    """«На продажу» из карточки склада: цена — закупка + стандартная наценка, дальше утверждает директор."""
    cat = db.get(PartCatalog, item.catalog_id)
    if not cat or cat.kind != "new":
        return "Выставить на продажу можно только новую запчасть"
    free = free_by_catalog(db, item.branch_id, [cat.id]).get(cat.id, D0)
    if qty <= 0 or qty > free:
        return f"Свободно на складе только {free.normalize():f} — больше выставить нельзя"
    if db.scalar(select(SiteNew.id).where(SiteNew.branch_id == item.branch_id, SiteNew.catalog_id == cat.id,
                                          SiteNew.status == "on", SiteNew.state != "declined").limit(1)):
        return "Эта запчасть уже выставлена на продажу — «На сайт → Новые»"
    markup = Decimal(str(st.get(db, item.branch_id, "pricing").get("default_markup", 25)))
    price = ssvc.money((item.cost or D0) * (1 + markup / 100))
    if price <= 0:
        return "У позиции нет закупочной цены — цену не посчитать"
    sn = SiteNew(catalog_id=cat.id, code=next_code(db), qty=qty, price=price, source="manual",
                 branch_id=item.branch_id)
    db.add(sn)
    db.flush()
    return sn


# ---------- Сколько можно продать ----------
def free_by_catalog(db: Session, branch_id, catalog_ids) -> dict:
    items = db.scalars(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.catalog_id.in_(list(catalog_ids)))).all()
    bal = ssvc.balances(db, branch_id, [i.id for i in items]) if items else {}
    out: dict = {}
    for i in items:
        out[i.catalog_id] = out.get(i.catalog_id, D0) + max(bal[i.id]["free"], D0) if i.id in bal else out.get(i.catalog_id, D0)
    return out


def sold_qty(db: Session, ids) -> dict:
    """Сколько уже продано / отправлено / в резерве под покупателя по каждой новой детали."""
    rows = db.execute(select(UsedSale.site_new_id, func.sum(UsedSale.qty)).where(
        UsedSale.site_new_id.in_(list(ids)), UsedSale.status.in_(LIVE_SALES)).group_by(UsedSale.site_new_id)).all()
    return {k: Decimal(v or 0) for k, v in rows}


def rows(db: Session, branch_id) -> list[dict]:
    """Новые детали с этапом: ждут утверждения / на сайте / нет на складе / отказано / продано / сняты."""
    sns = db.scalars(select(SiteNew).where(SiteNew.branch_id == branch_id).order_by(SiteNew.created_at.desc())).all()
    if not sns:
        return []
    cats = {c.id: c for c in db.scalars(select(PartCatalog).where(PartCatalog.id.in_([s.catalog_id for s in sns])))}
    free = free_by_catalog(db, branch_id, {s.catalog_id for s in sns})
    sold = sold_qty(db, [s.id for s in sns])
    photos: dict = {}
    for p in db.scalars(select(SiteNewPhoto).where(SiteNewPhoto.site_new_id.in_([s.id for s in sns])).order_by(SiteNewPhoto.created_at)):
        photos.setdefault(p.site_new_id, []).append(p)
    orders = {w.id: w for w in db.scalars(select(WorkOrder).where(WorkOrder.id.in_([s.work_order_id for s in sns if s.work_order_id])))}
    out = []
    for s in sns:
        left = max((s.qty or D0) - sold.get(s.id, D0), D0)
        avail = min(left, free.get(s.catalog_id, D0))
        if s.status == "off":
            stage = "off"
        elif left <= 0:
            stage = "sold"
        elif s.state == "pending":
            stage = "wait"
        elif s.state == "declined":
            stage = "declined"
        elif avail <= 0:
            stage = "nostock"
        else:
            stage = "site"
        out.append({"s": s, "c": cats.get(s.catalog_id), "left": left, "avail": avail, "sold": sold.get(s.id, D0),
                    "stage": stage, "photos": photos.get(s.id, []), "wo": orders.get(s.work_order_id)})
    return out


def counts(db: Session, branch_id, sync: bool = True) -> dict:
    """Сколько новых деталей на каждом этапе (+ «nophoto» — на сайте, но без фото)."""
    if sync:
        sync_leftovers(db, branch_id)
    out = {k: 0 for k in STAGES}
    out["nophoto"] = 0
    for r in rows(db, branch_id):
        out[r["stage"]] += 1
        if r["stage"] == "site" and not r["photos"]:
            out["nophoto"] += 1
    return out


# ---------- Утверждение цены ----------
def approve(db: Session, s: SiteNew, user) -> str:
    if s.state == "approved":
        return "Уже утверждено"
    if (s.price or D0) <= 0:
        return "У детали нет цены — утвердить нельзя"
    s.state, s.answer, s.decided_by, s.decided_at = "approved", "", user.id, now()
    return ""


def decline(db: Session, s: SiteNew, user, answer: str) -> str:
    if not answer.strip():
        return "Напишите причину отказа — кладовщик разбора увидит её у детали"
    s.state, s.answer, s.decided_by, s.decided_at = "declined", answer.strip()[:1000], user.id, now()
    return ""


# ---------- Продажа ----------
def pick_item(db: Session, branch_id, s: SiteNew, qty: Decimal) -> StockItem | None:
    """Позиция склада этой запчасти, где хватает свободного остатка."""
    items = db.scalars(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.catalog_id == s.catalog_id)).all()
    bal = ssvc.balances(db, branch_id, [i.id for i in items]) if items else {}
    return next((i for i in items if i.id in bal and bal[i.id]["free"] >= qty), None)


def sell(db: Session, branch_id, s: SiteNew, qty: Decimal, action: str, **kw):
    """Резерв под покупателя или сразу «продано» — как у б/у. Цена — утверждённая, за штуку × количество."""
    from . import razbor as rsvc
    if s.status != "on":
        return "Деталь снята с продажи"
    if s.state != "approved":
        return "Цена ещё не утверждена директором — продать нельзя"
    r = next((x for x in rows(db, branch_id) if x["s"].id == s.id), None)
    if not r or qty <= 0 or qty > r["avail"]:
        return f"Можно продать не больше {(r['avail'] if r else D0).normalize():f} шт"
    item = pick_item(db, branch_id, s, qty)
    if not item:
        return "На складе нет свободной детали в одной ячейке — проверьте остатки"
    price = ssvc.money(s.price * qty)
    if action == "sold":
        res = rsvc.sell_now(db, branch_id, item, qty, price, kw.get("buyer_name", ""), kw.get("buyer_phone", ""),
                            kw.get("delivery", "pickup"), kw.get("pay_method", "cash"), ttn=kw.get("ttn", ""))
        if not isinstance(res, str):
            res.paid = True
    else:
        res = rsvc.reserve_for_buyer(db, branch_id, item, qty, price, kw.get("buyer_name", ""), kw.get("buyer_phone", ""),
                                     kw.get("delivery", "pickup"), pay_method=kw.get("pay_method", "cash"),
                                     comment=kw.get("comment", ""), np_address=kw.get("np_address", ""),
                                     cod=kw.get("cod", False), channel=kw.get("channel", ""))
    if isinstance(res, str):
        return res
    res.site_new_id = s.id
    return res


# ---------- Выгрузка для сайта ----------
def export_rows(db: Session, branch_id) -> list[dict]:
    """Новые на сайте: утверждено, выставлено, есть свободный остаток, галочка «на сайт»."""
    out = []
    for r in rows(db, branch_id):
        s, c = r["s"], r["c"]
        if r["stage"] != "site" or not s.publish or not c:
            continue
        out.append({"Тип": "Новая", "Код": s.code, "Название": c.name, "Категория": c.category, "Марка": "",
                    "Модель": "", "Год": "", "Двигатель": "", "КПП": "", "Кузов": "", "Состояние": "Новая",
                    "Цена, грн": float(s.price), "Количество": float(r["avail"]),
                    "Описание": " · ".join(x for x in (s.description, f"Бренд: {c.brand}" if c.brand else "",
                                                       f"Артикул: {c.article}" if c.article else "",
                                                       f"Подходит: {c.fits}" if c.fits else "") if x),
                    "Донор": "",
                    "Фото": ", ".join(f"{s.code}_{n + 1}.{p.file.rsplit('.', 1)[-1]}" for n, p in enumerate(r["photos"])),
                    "_photos": [p.file for p in r["photos"]]})
    return out
