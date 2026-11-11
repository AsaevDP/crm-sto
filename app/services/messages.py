"""Сообщения по наряду (11.11.2026, решение брата): общение команды наряда и запрос запчасти слесарем.

Как работает запрос запчасти:
  1. Слесарь смотрит склад / разбор (название, наличие, количество — без цен), выбирает то, что ЕСТЬ в наличии,
     свою открытую машину (наряд), количество, комментарий → «Попросить».
  2. Менеджеру наряда — сообщение с кнопками «Согласовать» / «Отказать» (цену для клиента он видит и может поправить).
     Кладовщику — то же сообщение «для сведения»: выдавать пока нельзя.
  3. Согласовано → запчасть в наряде, в резерве, и уходит кладовщику в «Запросы на выдачу» (обычная выдача).
     Отказано → слесарю сообщение с причиной.
Чего нет на складе — слесарь не выбирает, а пишет менеджеру / запчастисту текстом: «нужно заказать …» —
дальше обычный путь заказа. Инструмент сюда не идёт: его слесарь просит у кладовщика напрямую.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import (IssueRequest, IssueRequestLine, OrderMessage, PartCatalog, StockItem, User, WoPart, WorkOrder,
                      WoWork)
from . import notify as nsvc
from . import orders as osvc
from . import stock as ssvc

# В каких состояниях наряда слесарь может просить запчасть: машина у нас и ремонт ещё не закончен
ASK_STATUSES = ("diag", "approve", "picking", "waiting", "instock", "work")
# Кому можно писать по наряду: пока наряд не закрыт
OPEN_STATUSES = ("created", "diag", "approve", "picking", "waiting", "instock", "work", "ready", "paid")


def my_orders(ctx, statuses=ASK_STATUSES) -> list[WorkOrder]:
    """Открытые наряды, где я в команде: веду, подбираю, ремонтирую (в т. ч. отдельная работа)."""
    me = ctx.user.id
    q = select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, WorkOrder.status.in_(statuses), or_(
        WorkOrder.mechanic_id == me, WorkOrder.manager_id == me, WorkOrder.partsman_id == me,
        WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me))))
    return list(ctx.db.scalars(q.order_by(WorkOrder.created_at.desc())))


def team(db: Session, wo: WorkOrder) -> dict:
    """Кто в команде наряда: {"manager": User, "partsman": User, "mechanic": User}."""
    return {k: db.get(User, uid) if uid else None for k, uid in
            (("manager", wo.manager_id), ("partsman", wo.partsman_id), ("mechanic", wo.mechanic_id))}


def storekeepers(db: Session, branch_id) -> list:
    from ..permissions import effective_permissions
    return [u.id for u in db.scalars(select(User).where(User.branch_id == branch_id, User.active.is_(True)))
            if "stock.issue" in effective_permissions(db, u)]


def label(db: Session, wo: WorkOrder) -> str:
    return f"{nsvc.car(db, wo)} · наряд {wo.number}"


def send_text(db: Session, ctx, wo: WorkOrder, to: list, text: str) -> str:
    """Текстовое сообщение команде наряда. Ошибка или ''."""
    text = (text or "").strip()
    to = [u for u in dict.fromkeys(to) if u and u != ctx.user.id]
    if not text:
        return "Напишите сообщение"
    if not to:
        return "Кому? У наряда не назначен этот сотрудник"
    m = OrderMessage(work_order_id=wo.id, kind="text", to_ids=[str(u) for u in to], text=text[:1000])
    db.add(m)
    db.flush()
    nsvc.send(db, to, "msg", f"{ctx.user.short_name}: {text[:120]}", label(db, wo), f"/notifications#m-{m.id}", wo, skip=ctx.user.id)
    return ""


def ask_part(db: Session, ctx, wo: WorkOrder, item: StockItem, qty: Decimal, text: str) -> str:
    """Слесарь просит запчасть со склада под наряд. Ошибка или ''."""
    if wo.status not in ASK_STATUSES:
        return "По этой машине уже нельзя просить запчасти (ремонт закончен или наряд закрыт)"
    if qty <= 0:
        return "Сколько нужно?"
    free = ssvc.catalog_free(db, ctx.branch.id, [item.catalog_id])[item.catalog_id]
    if free < qty:
        return (f"На складе свободно только {free.normalize():f} — столько и можно попросить. "
                f"Если нужно больше — напишите менеджеру, он закажет") if free > 0 else \
            "Этой запчасти нет в наличии — напишите менеджеру или запчастисту, чтобы заказали"
    if not wo.manager_id:
        return "У наряда нет менеджера — некому согласовать"
    cat = db.get(PartCatalog, item.catalog_id)
    m = OrderMessage(work_order_id=wo.id, kind="part", to_ids=[str(wo.manager_id)], text=(text or "").strip()[:1000],
                     stock_item_id=item.id, catalog_id=item.catalog_id, qty=qty, status="pending")
    db.add(m)
    db.flush()
    what = f"{cat.name} × {qty.normalize():f}"
    nsvc.send(db, [wo.manager_id], "part_ask", f"{ctx.user.short_name} просит: {what} — согласуйте",
              label(db, wo) + (f" · «{m.text[:80]}»" if m.text else ""), f"/notifications#m-{m.id}", wo, skip=ctx.user.id)
    nsvc.send(db, storekeepers(db, ctx.branch.id), "part_ask_info", f"Слесарь просит: {what} — ждёт согласования менеджера",
              label(db, wo), "/stock/issues", wo, skip=ctx.user.id)
    return ""


def client_price(db: Session, branch_id, item: StockItem) -> Decimal:
    """Цена для клиента по умолчанию: б/у — цена разбора, новая — закупка × стандартная наценка."""
    cat = db.get(PartCatalog, item.catalog_id)
    if cat and cat.kind == "used":
        return ssvc.money(item.sale_price or 0)
    from . import settings as st
    return ssvc.sale_price(item.cost, st.get(db, branch_id, "pricing")["default_markup"])


def can_decide(ctx, wo: WorkOrder) -> bool:
    """Согласует менеджер наряда (или админ / тот, кто видит все наряды и согласует с клиентами)."""
    return ctx.can("orders.approve") and (wo.manager_id == ctx.user.id or ctx.user.role.code == "admin"
                                          or osvc.sees_all_orders(ctx))


def approve(db: Session, ctx, m: OrderMessage, price=None, answer: str = "") -> str:
    """Менеджер согласовал: запчасть — в наряд и в резерв, кладовщику — на выдачу. Ошибка или ''."""
    wo = db.get(WorkOrder, m.work_order_id)
    if m.status != "pending":
        return "По этому запросу уже решили"
    if wo.status not in ASK_STATUSES:
        return "Наряд уже не в ремонте — запчасть сюда не добавить"
    item = db.get(StockItem, m.stock_item_id)
    cat = db.get(PartCatalog, m.catalog_id)
    used = cat.kind == "used"
    p = ssvc.money(price) if price is not None and Decimal(price) >= 0 else client_price(db, ctx.branch.id, item)
    cost = ssvc.money(item.sale_price if used else item.cost)
    markup = 0 if used or not cost else max(0, min(1000, int(round((p / cost - 1) * 100))))
    part = WoPart(work_order_id=wo.id, catalog_id=cat.id, name=cat.name, qty=m.qty, cost=cost, markup_pct=markup, price=p)
    db.add(part)
    db.flush()
    if not ssvc.reserve_from_stock(db, ctx.branch.id, part):
        db.delete(part)
        return "На складе этой запчасти уже не хватает — её успели забрать. Откажите и попросите запчастиста заказать"
    m.status, m.decided_by, m.decided_at, m.answer, m.wo_part_id = "approved", ctx.user.id, now(), answer.strip()[:300], part.id
    req = issue_one(db, wo, part, f"Попросил слесарь, согласовал {ctx.user.short_name}"
                                  + (f": {m.text[:200]}" if m.text else ""), m.created_by)
    osvc.event(db, wo, "status", f"Согласована запчасть по просьбе слесаря: {cat.name} × {Decimal(m.qty).normalize():f}")
    what = f"{cat.name} × {Decimal(m.qty).normalize():f}"
    nsvc.send(db, [m.created_by], "part_ok", f"Согласовано: {what}", label(db, wo) + " · кладовщик выдаст",
              f"/orders/{wo.id}#parts", wo, skip=ctx.user.id)
    nsvc.send(db, storekeepers(db, ctx.branch.id), "part_go", f"Согласовано — выдать: {what}", label(db, wo),
              "/stock/issues", wo, skip=ctx.user.id)
    _close_notes(db, m)
    return ""


def decline(db: Session, ctx, m: OrderMessage, answer: str) -> str:
    wo = db.get(WorkOrder, m.work_order_id)
    if m.status != "pending":
        return "По этому запросу уже решили"
    if not answer.strip():
        return "Напишите слесарю, почему нет"
    cat = db.get(PartCatalog, m.catalog_id)
    m.status, m.decided_by, m.decided_at, m.answer = "declined", ctx.user.id, now(), answer.strip()[:300]
    nsvc.send(db, [m.created_by], "part_no", f"Отказано: {cat.name if cat else ''}", f"{label(db, wo)} · {m.answer}",
              f"/notifications#m-{m.id}", wo, skip=ctx.user.id)
    nsvc.send(db, storekeepers(db, ctx.branch.id), "part_ask_info", f"Не выдавать: {cat.name if cat else ''} — менеджер отказал",
              label(db, wo), "/stock/issues", wo, skip=ctx.user.id)
    _close_notes(db, m)
    return ""


def _close_notes(db: Session, m: OrderMessage):
    """Окошки «просит — согласуйте» и «ждёт согласования» по этому запросу больше не нужны."""
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.work_order_id == m.work_order_id,
                                                   Notification.kind.in_(("part_ask", "part_ask_info")),
                                                   Notification.read_at.is_(None))):
        if n.url.endswith(f"m-{m.id}") or n.kind == "part_ask_info":
            n.read_at = now()


def issue_one(db: Session, wo: WorkOrder, part: WoPart, comment: str, asked_by=None) -> IssueRequest:
    """Запрос на выдачу только этой позиции (не всего, что лежит в резерве по наряду)."""
    req = db.scalar(select(IssueRequest).where(IssueRequest.work_order_id == wo.id, IssueRequest.status == "new"))
    if req is None:
        req = IssueRequest(work_order_id=wo.id, comment=comment[:500])
        db.add(req)
        db.flush()
        if asked_by:
            req.created_by = asked_by   # «просит» у кладовщика — слесарь
    db.add(IssueRequestLine(request_id=req.id, wo_part_id=part.id, qty=part.qty))
    return req


def pending_parts(db: Session, branch_id) -> list[OrderMessage]:
    return list(db.scalars(select(OrderMessage).where(OrderMessage.branch_id == branch_id, OrderMessage.kind == "part",
                                                      OrderMessage.status == "pending").order_by(OrderMessage.created_at)))


def visible(ctx, limit: int = 100) -> list[OrderMessage]:
    """Мои сообщения: написал я, написали мне, или (менеджеру, видящему наряды) — по моим нарядам."""
    me = str(ctx.user.id)
    rows = ctx.db.scalars(select(OrderMessage).where(OrderMessage.branch_id == ctx.branch.id)
                          .order_by(OrderMessage.created_at.desc()).limit(400)).all()
    out = []
    for m in rows:
        if m.created_by == ctx.user.id or me in (m.to_ids or []):
            out.append(m)
        if len(out) >= limit:
            break
    return out


def for_order(db: Session, wo: WorkOrder) -> list[OrderMessage]:
    return list(db.scalars(select(OrderMessage).where(OrderMessage.work_order_id == wo.id).order_by(OrderMessage.created_at)))


def fmt_when(dt: datetime) -> str:
    return dt.astimezone().strftime("%d.%m %H:%M") if dt else ""
