"""«Заявки с сайта» (22.11.2026, брат). К сайту CRM не подключается сама: сайт присылает заявку на адрес
POST /api/site/orders (с ключом из «Настроек»), а пока сайта нет — заявку вписывают руками («＋ Заявка с сайта»).

Заявка: покупатель (фамилия, имя, телефон), детали по коду с сайта (Р-0001 — б/у разбора, Н-0001 — новая из
«На сайт»), получение (Нова Пошта: город + отделение / курьер: адрес / заберёт лично), оплата, комментарий.
«✓ Принять» — каждая деталь в резерв под покупателя (UsedSale, откуда = «сайт») → «Выдача и отправка».
Хоть одной детали нет в наличии — не принимаем (сначала позвонить покупателю). «Отказать» — с причиной."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PartCatalog, SiteNew, SiteOrder, SiteOrderLine, StockItem
from . import orders as osvc
from . import stock as ssvc

D0 = Decimal(0)


def next_number(db: Session, branch_id) -> int:
    return (db.scalar(select(func.max(SiteOrder.number)).where(SiteOrder.branch_id == branch_id)) or 0) + 1


def count_new(db: Session, branch_id) -> int:
    return db.scalar(select(func.count()).select_from(SiteOrder).where(SiteOrder.branch_id == branch_id,
                                                                       SiteOrder.status == "new")) or 0


def _qty(v) -> Decimal:
    try:
        q = Decimal(str(v or "1").replace(",", ".").strip() or "1")
    except Exception:
        q = Decimal(1)
    return q if q > 0 else Decimal(1)


def _money(v) -> Decimal:
    try:
        return ssvc.money(Decimal(str(v or "0").replace(",", ".").replace(" ", "").strip() or "0"))
    except Exception:
        return D0


def check(data: dict, lines: list[dict]) -> list[str]:
    e = []
    if not (data.get("last_name") or "").strip() and not (data.get("first_name") or "").strip():
        e.append("Фамилия и имя покупателя")
    if len(osvc.norm_phone(data.get("phone") or "")) < 10:
        e.append("Телефон покупателя")
    if not [l for l in lines if (l.get("code") or "").strip()]:
        e.append("Хотя бы одна деталь (код с сайта: Р-0001 или Н-0001)")
    d = data.get("delivery") or "np"
    if d == "np" and not ((data.get("city") or "").strip() and (data.get("np_branch") or "").strip()):
        e.append("Нова Пошта: город и отделение")
    if d == "courier" and not (data.get("address") or "").strip():
        e.append("Курьер: адрес доставки")
    return e


def create(db: Session, branch_id, data: dict, lines: list[dict], source: str = "manual") -> SiteOrder | list[str]:
    """Новая заявка. Ошибки — список того, что не заполнено."""
    from ..models.razbor import ORDER_DELIVERY, ORDER_PAY
    errs = check(data, lines)
    if errs:
        return errs
    o = SiteOrder(number=next_number(db, branch_id), last_name=(data.get("last_name") or "").strip()[:80],
                  first_name=(data.get("first_name") or "").strip()[:80], phone=osvc.norm_phone(data.get("phone") or ""),
                  delivery=data.get("delivery") if data.get("delivery") in ORDER_DELIVERY else "np",
                  city=(data.get("city") or "").strip()[:80], np_branch=(data.get("np_branch") or "").strip()[:80],
                  address=(data.get("address") or "").strip()[:200],
                  pay=data.get("pay") if data.get("pay") in ORDER_PAY else "cod",
                  comment=(data.get("comment") or "").strip()[:2000], source=source if source in ("site", "manual") else "manual")
    db.add(o)
    db.flush()
    for l in lines[:30]:
        code = (l.get("code") or "").strip().upper().replace("P-", "Р-").replace("H-", "Н-")[:20]
        if code:
            db.add(SiteOrderLine(site_order_id=o.id, code=code, name=(l.get("name") or "").strip()[:200],
                                 qty=_qty(l.get("qty")), price=_money(l.get("price"))))
    db.flush()
    return o


def resolve(db: Session, branch_id, line: SiteOrderLine) -> dict:
    """Что за деталь по коду и можно ли её отложить. {"kind", "item"|"sn", "name", "price", "ok", "why"}."""
    code = (line.code or "").strip().upper()
    if code.startswith("Н-"):
        sn = db.scalar(select(SiteNew).where(SiteNew.branch_id == branch_id, SiteNew.code == code))
        if not sn:
            return {"kind": "new", "name": line.name or code, "price": line.price, "ok": False, "why": "нет такого кода"}
        cat = db.get(PartCatalog, sn.catalog_id)
        from . import site as site_svc
        r = next((x for x in site_svc.rows(db, branch_id) if x["s"].id == sn.id), None)
        avail = r["avail"] if r else D0
        why = ("снята с продажи" if sn.status != "on" else "цена не утверждена" if sn.state != "approved"
               else "нет в наличии" if avail < line.qty else "")
        return {"kind": "new", "sn": sn, "name": cat.name if cat else code, "price": ssvc.money(sn.price * line.qty),
                "ok": not why, "why": why}
    item = db.scalar(select(StockItem).where(StockItem.branch_id == branch_id, StockItem.code == code))
    if not item:
        return {"kind": "used", "name": line.name or code, "price": line.price, "ok": False, "why": "нет такого кода"}
    cat = db.get(PartCatalog, item.catalog_id)
    from . import razbor_price as rp
    free = ssvc.balances(db, branch_id, [item.id])[item.id]["free"]
    why = rp.block_error(item) if rp.blocked(item) else ("нет в наличии (продана, в резерве или в наряде)" if free < line.qty else "")
    return {"kind": "used", "item": item, "name": cat.name if cat else code, "price": ssvc.money(item.sale_price * line.qty),
            "ok": not why, "why": why}


def lines_of(db: Session, branch_id, o: SiteOrder) -> list[dict]:
    out = []
    for l in db.scalars(select(SiteOrderLine).where(SiteOrderLine.site_order_id == o.id).order_by(SiteOrderLine.created_at)):
        r = resolve(db, branch_id, l) if o.status == "new" else {"name": l.name or l.code, "price": l.price, "ok": True, "why": ""}
        r["l"] = l
        out.append(r)
    return out


def accept(db: Session, branch_id, o: SiteOrder, user_id) -> str:
    """«✓ Принять»: все детали — в резерв под покупателя (откуда = сайт). Ошибка или ''."""
    from . import razbor as rsvc
    from . import site as site_svc
    if o.status != "new":
        return "Заявка уже обработана"
    rows = lines_of(db, branch_id, o)
    bad = [r for r in rows if not r["ok"]]
    if bad:
        return "Нельзя принять: " + "; ".join(f"{r['l'].code} — {r['why']}" for r in bad[:3]) + \
               ". Позвоните покупателю и уберите деталь из заявки или откажите"
    delivery = {"np": "np", "courier": "other", "pickup": "pickup"}[o.delivery]
    where = f"{o.city}, {o.np_branch}" if o.delivery == "np" else (o.address if o.delivery == "courier" else "")
    common = dict(buyer_name=o.buyer, buyer_phone=o.phone, delivery=delivery, pay_method="card" if o.pay == "card" else "cash",
                  comment=f"Заявка с сайта {o.code}" + (f". {o.comment}" if o.comment else ""), np_address=where,
                  cod=o.pay == "cod" and o.delivery != "pickup", channel="site")
    for r in rows:
        l = r["l"]
        if r["kind"] == "new":
            res = site_svc.sell(db, branch_id, r["sn"], l.qty, "reserve", **common)
        else:
            price = l.price if l.price > 0 else r["price"]
            res = rsvc.reserve_for_buyer(db, branch_id, r["item"], l.qty, price, common["buyer_name"], common["buyer_phone"],
                                         delivery, pay_method=common["pay_method"], comment=common["comment"],
                                         np_address=where, cod=common["cod"], channel="site")
        if isinstance(res, str):
            return f"{l.code}: {res}"
        l.sale_id = res.id
        l.name = l.name or r["name"]
        l.price = res.price
    o.status, o.decided_by, o.decided_at = "accepted", user_id, now()
    return ""


def decline(db: Session, o: SiteOrder, reason: str, user_id) -> str:
    if o.status != "new":
        return "Заявка уже обработана"
    if not reason.strip():
        return "Укажите причину отказа"
    o.status, o.answer, o.decided_by, o.decided_at = "declined", reason.strip()[:500], user_id, now()
    return ""


def remove_line(db: Session, o: SiteOrder, line_id) -> None:
    """Покупатель согласился без этой детали — убрать строку (только у новой заявки)."""
    l = db.get(SiteOrderLine, line_id)
    if o.status == "new" and l and l.site_order_id == o.id:
        db.delete(l)
