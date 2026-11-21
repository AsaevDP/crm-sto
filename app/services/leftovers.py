"""Отказ клиента от детали после согласования и «Лишнее после отказов» (19.11.2026, брат).

До «Согласовал с клиентом» — обычное «Убрать» (в архив наряда, причина не нужна: клиент ещё ничего не согласовывал).
После согласования — «Клиент отказался» с обязательной причиной:
  А. ещё не заказана у поставщика (черновик, «нужна») — сразу в архив наряда, строка черновика уходит;
  Б. уже заказана — в архив наряда, а в «Лишнее после отказов» строка «решает запчастист»: «Отменил у поставщика»
     (строка закупки снимается) или «Пусть приедет — на склад» (приедет свободной, не под наряд);
  В. приехала и лежит в резерве — резерв снят, деталь свободна на складе → в «Лишнее после отказов»;
  Г. выдана мастеру — сначала «Вернуть на склад».
Лишнее на складе: «Оставить на складе» (подбирается в другие машины как обычно), «Вернуть поставщику» (списание со
склада с пометкой «возврат поставщику»), «Продать» (→ «На сайт → Новые», services/site.py: цена для клиента, утверждает директор).
Решает запчастист (parts.pick), кладовщик видит список и тоже может отметить (stock.manage)."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PartLeftover, Purchase, PurchaseLine, StockItem, User, WoPart, WorkOrder
from . import notify as nsvc

D0 = Decimal(0)
STATUS = {"decide": ("заказана — решает запчастист", "red"), "wait": ("едет — ляжет на склад", "orange"),
          "stock": ("на складе — решить", "orange"), "kept": ("оставили на складе", "gray"),
          "sale": ("на продажу", "teal"), "returned": ("вернули поставщику", "gray"),
          "cancelled": ("отменили у поставщика", "gray"), "undone": ("клиент передумал — снова в наряде", "gray")}
OPEN = ("decide", "wait", "stock")
ACTIVE = ("decide", "wait", "stock", "kept", "sale")   # показываем в списке


def after_approval(wo: WorkOrder) -> bool:
    from .stock import PLAN_STATUSES
    return wo.status not in PLAN_STATUSES


def refuse(db: Session, wo: WorkOrder, part: WoPart, user: User, reason: str) -> str:
    """«Клиент отказался» после согласования. Ошибка или ''."""
    from . import stock as ssvc
    reason = (reason or "").strip()
    if not reason:
        return "Напишите причину отказа клиента — она останется в наряде"
    if part.status in ("issued", "installed"):
        return "Запчасть уже выдана мастеру — сначала «Вернуть на склад»"
    if part.status in ("archived", "returned", "declined", "option"):
        return "Эта позиция уже не в наряде"
    from ..models import IssueRequest, IssueRequestLine
    was_reserved = part.status == "reserved"
    asked = db.scalar(select(IssueRequest).join(IssueRequestLine, IssueRequestLine.request_id == IssueRequest.id).where(
        IssueRequestLine.wo_part_id == part.id, IssueRequest.status.in_(("new", "ready"))))
    sent = ssvc.sent_lines(db, part)
    # что уже приехало под эту деталь и лежит в резерве. Взятое с полки (source="from_stock") просто вернётся на
    # полку свободным — это не «лишнее», его не заказывали специально
    on_stock = ssvc.reserved_for_part(db, part.id) if (part.source or "") != "from_stock" else {}
    got = sum(on_stock.values(), D0)
    cat_cost = part.cost or D0
    err = ssvc.archive_part(db, part, user.id, tell=False)   # резерв снят, черновик убран, заказанное — отвязано
    if err:
        return err
    part.refuse_reason = reason[:500]
    left_qty = Decimal(part.qty) - got
    if sent and left_qty > 0:   # Б: заказано у поставщика и ещё не всё приехало — решает запчастист
        db.add(PartLeftover(work_order_id=wo.id, wo_part_id=part.id, catalog_id=part.catalog_id,
                            purchase_line_id=sent[0].id, qty=left_qty, cost=cat_cost, reason=reason, status="decide"))
        nsvc.send(db, [wo.partsman_id], "refused", f"Клиент отказался: {part.name} — заказ у поставщика",
                  f"{nsvc.car(db, wo)} · наряд {wo.number} · {reason[:150]}. Отмените у поставщика или пусть приедет на склад",
                  "/stock/leftovers", wo, skip=user.id)
    if got > 0:   # В: уже на складе — свободная деталь СТО
        db.add(PartLeftover(work_order_id=wo.id, wo_part_id=part.id, catalog_id=part.catalog_id, qty=got,
                            cost=cat_cost, reason=reason, status="stock"))
        nsvc.send(db, [wo.partsman_id], "refused", f"Клиент отказался: {part.name} — лежит на складе",
                  f"{nsvc.car(db, wo)} · наряд {wo.number} · {reason[:150]}. Оставить, вернуть поставщику или продать",
                  "/stock/leftovers", wo, skip=user.id)
    # 19.11.2026 (брат): у каждого по цепочке — что делать. Мастерам наряда: не ставить. Кладовщику: не выдавать
    # (деталь уже ушла из запроса на выдачу; запрос без деталей закрываем).
    nsvc.send(db, nsvc.masters(db, wo), "refused_info", f"Клиент отказался: {part.name} — не ставить",
              f"{nsvc.car(db, wo)} · наряд {wo.number} · {reason[:150]}", f"/orders/{wo.id}#parts", wo, skip=user.id)
    if asked or was_reserved:
        from .messages import storekeepers
        nsvc.send(db, storekeepers(db, wo.branch_id, "razbor" if asked and asked.zone == "razbor" else "new"), "refused_info", f"Клиент отказался: {part.name} — не выдавать",
                  f"{nsvc.car(db, wo)} · наряд {wo.number}" + (" · убрана из запроса на выдачу" if asked else "")
                  + " · деталь остаётся на складе свободной", "/stock/issues", wo, skip=user.id)
    if asked:
        db.flush()
        if not db.scalar(select(IssueRequestLine.id).where(IssueRequestLine.request_id == asked.id).limit(1)):
            asked.status = "cancelled"   # в запросе больше нечего выдавать
    from .orders import event
    event(db, wo, "status", f"Клиент отказался от «{part.name}»: {reason[:200]}")
    db.flush()
    return ""


def _line(db: Session, lo: PartLeftover) -> PurchaseLine | None:
    return db.get(PurchaseLine, lo.purchase_line_id) if lo.purchase_line_id else None


def arrived(db: Session, lo: PartLeftover) -> bool:
    l = _line(db, lo)
    return bool(l and (l.received or (l.received_qty or 0) > 0))


def decide(db: Session, lo: PartLeftover, user: User, action: str, note: str = "") -> str:
    """Запчастист / кладовщик решает. action: cancel / to_stock / keep / return / sale. Ошибка или ''."""
    wo = db.get(WorkOrder, lo.work_order_id)
    if lo.status == "decide" and action == "cancel":
        l = _line(db, lo)
        if l and arrived(db, lo):
            return "Деталь уже приехала и принята на склад — отменить нельзя. Оставьте, верните поставщику или продайте"
        if l:
            from . import stock as ssvc
            p = db.get(Purchase, l.purchase_id)
            db.delete(l)
            db.flush()
            if p and not db.scalar(select(PurchaseLine.id).where(PurchaseLine.purchase_id == p.id).limit(1)):
                db.delete(p)   # в накладной больше ничего — закупки нет
            elif p:
                ssvc.refresh_purchase_status(db, p)
        lo.status = "cancelled"
        msg = "отменил заказ у поставщика"
    elif lo.status == "decide" and action == "to_stock":
        lo.status = "stock" if arrived(db, lo) else "wait"
        msg = "отменить нельзя — приедет на склад"
    elif lo.status in ("stock", "kept", "sale") and action == "keep":
        lo.status, msg = "kept", "оставил на складе"
    elif lo.status in ("stock", "kept", "sale") and action == "sale":
        lo.status, msg = "sale", "на продажу"
    elif lo.status in ("stock", "kept", "sale") and action == "return":
        err = _writeoff(db, lo, user)
        if err:
            return err
        lo.status, msg = "returned", "вернул поставщику"
    else:
        return "Это действие сейчас недоступно"
    lo.decided_by, lo.decided_at, lo.note = user.id, now(), (note or "").strip()[:300]
    part = db.get(WoPart, lo.wo_part_id)
    nsvc.send(db, [wo.manager_id], "refused_done", f"Отказ клиента: {part.name if part else ''} — {msg}",
              f"{nsvc.car(db, wo)} · наряд {wo.number} · {user.short_name}", f"/orders/{wo.id}#parts", wo, skip=user.id)
    from .orders import event
    event(db, wo, "status", f"Отказ клиента от «{part.name if part else ''}»: {msg}" + (f" — {lo.note}" if lo.note else ""))
    # 22.11.2026 (брат): «Продать» — сразу в «На сайт → Новые» (цену утверждает директор); передумали — снята с продажи
    from . import site as site_svc
    db.flush()
    site_svc.sync_leftovers(db, lo.branch_id)
    return ""


def _writeoff(db: Session, lo: PartLeftover, user: User) -> str:
    """«Вернуть поставщику»: списываем со свободного остатка этой позиции (сколько было лишним)."""
    from . import stock as ssvc
    items = db.scalars(select(StockItem).where(StockItem.branch_id == lo.branch_id, StockItem.catalog_id == lo.catalog_id)).all()
    bal = ssvc.balances(db, lo.branch_id, [i.id for i in items])
    free = sum((bal[i.id]["free"] for i in items if i.id in bal), D0)
    if free < lo.qty:
        return f"На складе свободно только {free.normalize():f} — эту деталь уже взяли в другой наряд"
    left = Decimal(lo.qty)
    wo = db.get(WorkOrder, lo.work_order_id)
    for i in items:
        take = min(bal[i.id]["free"] if i.id in bal else D0, left)
        if take <= 0:
            continue
        ssvc.move(db, "writeoff", i, take, comment=f"Возврат поставщику — отказ клиента, наряд {wo.number if wo else ''}")
        left -= take
        if left <= 0:
            break
    return ""


def undo(db: Session, lo: PartLeftover):
    """Клиент передумал, пока решали: деталь возвращается в наряд — строку «лишнее» закрываем."""
    lo.status = "undone"


def rows(db: Session, branch_id, statuses=ACTIVE) -> list[dict]:
    los = db.scalars(select(PartLeftover).where(PartLeftover.branch_id == branch_id, PartLeftover.status.in_(statuses))
                     .order_by(PartLeftover.created_at.desc())).all()
    changed = False
    for lo in los:   # «приедет на склад» — приехало: теперь решить, что с ней делать
        if lo.status == "wait" and arrived(db, lo):
            lo.status, changed = "stock", True
    if changed:
        db.flush()
    out = []
    for lo in los:
        wo = db.get(WorkOrder, lo.work_order_id)
        from ..models import PartCatalog, Vehicle
        out.append({"lo": lo, "wo": wo, "v": db.get(Vehicle, wo.vehicle_id) if wo else None,
                    "part": db.get(WoPart, lo.wo_part_id), "cat": db.get(PartCatalog, lo.catalog_id) if lo.catalog_id else None,
                    "line": _line(db, lo), "by": db.get(User, lo.decided_by) if lo.decided_by else None})
    return out


def for_order(db: Session, wo_id) -> dict:
    """{wo_part_id: PartLeftover} — последнее решение по каждой отказанной детали наряда (видно в архиве наряда)."""
    out = {}
    for lo in db.scalars(select(PartLeftover).where(PartLeftover.work_order_id == wo_id).order_by(PartLeftover.created_at)):
        out[lo.wo_part_id] = lo
    return out
