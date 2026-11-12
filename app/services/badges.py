"""Цветные цифры в меню (12.11.2026): у каждого пункта — сколько ждёт именно этого сотрудника.

Цвета как на полосе этапов наряда: red — срочно / ждут вас, yellow — ждут действия, green — готово.
Несколько видов в одном пункте — цифры рядом («🟡 2 🟢 1»). Опрос — /api/notify раз в 15 секунд.
У каждой цифры своя ссылка — нажали, открылся раздел сразу с нужным фильтром.

Наряды: админ, директор, зам — все; остальные — свои (менеджер ведёт / слесарь / запчастист).
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from ..models import IssueRequest, Purchase, PurchaseLine, UsedSale, WoPart, WorkOrder
from ..permissions import MASTERS

ALL_ROLES = ("admin", "director", "deputy")


def _add(out: dict, href: str, color: str, n: int, title: str, link: str = ""):
    if not n:
        return
    lst = out.setdefault(href, [])
    same = next((x for x in lst if x["c"] == color), None)
    if same:   # одного цвета — одной цифрой, подсказка перечисляет всё
        same["n"] += int(n)
        same["t"] += f" · {title}"
    else:
        lst.append({"c": color, "n": int(n), "t": title, "href": link or href})
    lst.sort(key=lambda x: ("red", "yellow", "green").index(x["c"]))


def compute(ctx) -> dict:
    db, me, bid = ctx.db, ctx.user, ctx.branch.id
    role = me.role.code
    out: dict = {}
    see_all = role in ALL_ROLES

    # ---------- Слесарь просит запчасть (11.11.2026) — кладовщику «для сведения»; менеджеру цифра — у «Сообщений» ----------
    if ctx.can("stock.issue"):
        from ..models import OrderMessage
        _add(out, "/stock/issues", "yellow", db.scalar(select(func.count()).select_from(OrderMessage).where(
            OrderMessage.branch_id == bid, OrderMessage.kind == "part", OrderMessage.status == "pending")) or 0,
             "слесарь просит — ждёт согласования менеджера")

    # ---------- Наряды ----------
    if ctx.can("orders.view_all", "orders.view_own"):
        mine = or_(WorkOrder.manager_id == me.id, WorkOrder.mechanic_id == me.id, WorkOrder.partsman_id == me.id)

        def n_status(*sts, field=None):
            qq = select(func.count()).select_from(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(sts))
            if field is not None:
                qq = qq.where(field == me.id)
            elif not see_all:
                qq = qq.where(mine)
            return db.scalar(qq) or 0

        # 🔴 срочная запчасть ещё не выдана (менеджер, запчастист, админ)
        if ctx.can("orders.approve", "parts.pick"):
            uq = (select(func.count(func.distinct(WoPart.work_order_id))).join(WorkOrder, WorkOrder.id == WoPart.work_order_id)
                  .where(WorkOrder.branch_id == bid, WoPart.urgent.is_(True),
                         WoPart.status.in_(("need", "ordered", "reserved")), WorkOrder.status.not_in(("closed", "cancel"))))
            if not see_all:
                uq = uq.where(mine)
            _add(out, "/orders", "red", db.scalar(uq) or 0, "срочные запчасти", "/orders?tab=parts")
        if role in MASTERS:
            _add(out, "/orders", "red", n_status("created", "diag", field=WorkOrder.mechanic_id), "новые — посмотреть",
                 "/orders?tab=intake")
        if ctx.can("orders.approve"):   # 12.11.2026: мне передали наряд — горит, пока не нажал «Принял»
            _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(WorkOrder).where(
                WorkOrder.branch_id == bid, WorkOrder.manager_id == me.id, WorkOrder.handover_pending.is_(True),
                WorkOrder.status.not_in(("closed", "cancel")))) or 0, "вам передали наряд — нажмите «Принял»", "/orders?tab=handed")
        if ctx.can("orders.approve"):
            _add(out, "/orders", "yellow", n_status("approve", field=None if see_all else WorkOrder.manager_id),
                 "согласовать с клиентом", "/orders?tab=approve")
        if ctx.can("parts.pick") and not ctx.can("orders.approve"):
            _add(out, "/orders", "yellow", n_status("picking", field=None if see_all else WorkOrder.partsman_id),
                 "подобрать запчасти", "/orders?tab=parts")
        if ctx.can("orders.approve"):
            _add(out, "/orders", "green", n_status("ready", field=None if see_all else WorkOrder.manager_id),
                 "готовы к выдаче клиенту", "/orders?tab=ready")
        if role in MASTERS:
            _add(out, "/orders", "green", n_status("instock", field=WorkOrder.mechanic_id), "запчасти на складе — можно начинать",
                 "/orders?tab=parts")

    # 🔴 менеджер просит доступ ко всем нарядам — админу / директору / заму
    if ctx.can("orders.grant_access"):
        from ..models import OrderAccess
        _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(OrderAccess).where(
            OrderAccess.branch_id == bid, OrderAccess.status == "pending")) or 0, "просят доступ ко всем нарядам",
             "/access-requests")
        from ..models import ClientAccess   # 12.11.2026: менеджер просит доступ к клиенту
        _add(out, "/clients", "red", db.scalar(select(func.count()).select_from(ClientAccess).where(
            ClientAccess.branch_id == bid, ClientAccess.status == "pending")) or 0, "менеджер просит доступ к клиенту",
             "/access-requests#clients")
        from ..models import OrderReopen   # 11.11.2026: просят открыть закрытый наряд заново
        _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(OrderReopen).where(
            OrderReopen.branch_id == bid, OrderReopen.status == "pending")) or 0, "просят открыть наряд заново",
             "/access-requests#reopen")

    # ---------- Готово к выдаче (менеджер) ----------
    if ctx.can("stock.pickup") and not ctx.can("stock.issue"):
        n = 0
        for r, wo in db.execute(select(IssueRequest, WorkOrder).join(WorkOrder, WorkOrder.id == IssueRequest.work_order_id)
                                .where(IssueRequest.branch_id == bid, IssueRequest.status == "ready")):
            if wo.manager_id == me.id or r.created_by == me.id:
                n += 1
        _add(out, "/ready", "green", n, "кладовщик подготовил")

    # ---------- Запросы на выдачу (кладовщик) ----------
    if ctx.can("stock.issue", "tools.manage"):
        n = 0
        if ctx.can("stock.issue"):
            n += db.scalar(select(func.count()).select_from(IssueRequest).where(IssueRequest.branch_id == bid,
                                                                                IssueRequest.status == "new")) or 0
        if ctx.can("tools.manage"):
            from ..models import ToolRequest
            n += db.scalar(select(func.count()).select_from(ToolRequest).where(ToolRequest.branch_id == bid,
                                                                               ToolRequest.status == "new")) or 0
        _add(out, "/stock/issues", "red", n, "просят выдать")
        if ctx.can("stock.issue"):   # 12.11.2026: мойщик / детейлер просит химию
            from ..models import ChemRequest
            _add(out, "/stock/issues", "red", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "new")) or 0, "просят химию для мойки", "/stock/issues?tab=chem")
        if ctx.can("stock.issue"):
            _add(out, "/stock/issues", "green", db.scalar(select(func.count()).select_from(IssueRequest).where(
                IssueRequest.branch_id == bid, IssueRequest.status == "ready")) or 0, "готово — ждут, когда заберут")

    # ---------- Склад, Закупки (кладовщик) ----------
    if ctx.can("stock.manage"):
        _add(out, "/stock", "yellow", db.scalar(
            select(func.count()).select_from(PurchaseLine).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
            .where(Purchase.branch_id == bid, Purchase.status.in_(("ordered", "delivered", "partial")),
                   PurchaseLine.received.is_(False))) or 0, "ждём приход", "/stock/receive")
        _add(out, "/purchases", "yellow", db.scalar(select(func.count()).select_from(Purchase).where(
            Purchase.branch_id == bid, Purchase.status == "delivered")) or 0, "привезли — принять", "/purchases?tab=delivered")

    # ---------- Мои заказы (запчастист) ----------
    if ctx.can("purchases.mine") and not ctx.can("purchases.by_partsman"):
        from . import stock as ssvc
        rows = ssvc.partsman_lines(db, bid, me.id, days=1)
        _add(out, "/my-orders", "red", sum(1 for r in rows if r["late"]), "опаздывают", "/my-orders?st=late")
        # 12.11.2026: черновик под наряд не отправлен поставщику — в наряде это «в черновике», кладовщик ждёт
        _add(out, "/my-orders", "yellow", len(ssvc.draft_waits(db, bid, me.id)), "черновики под наряды — нажмите «✓ Заказал»",
             "/my-orders?st=draft")
        _add(out, "/my-orders", "yellow", sum(1 for r in rows if r["st"] == "delivered"), "привезли — кладовщик принимает",
             "/my-orders?st=delivered")
        _add(out, "/my-orders", "green", sum(1 for r in rows if r["st"] == "arrived"), "приняты на склад сегодня",
             "/my-orders?st=arrived")

    # ---------- Разбор: выдача и отправка ----------
    if ctx.can("razbor.manage"):
        base = select(func.count()).select_from(UsedSale).where(UsedSale.branch_id == bid, UsedSale.status == "reserved")
        _add(out, "/razbor/shipping", "red", db.scalar(base.where(UsedSale.packed_at.is_(None))) or 0, "подготовить",
             "/razbor/shipping?st=prepare")
        _add(out, "/razbor/shipping", "yellow", db.scalar(base.where(UsedSale.packed_at.is_not(None))) or 0,
             "готово — ждёт покупателя или отправки", "/razbor/shipping?st=ready_ship")

    # ---------- Зарплата: прошлая неделя ждёт проверки / утверждения ----------
    if ctx.can("payroll.check", "payroll.edit"):
        from . import payweek as wsvc
        from .staff import staff_for_period
        start = wsvc.monday(date.today()) - timedelta(days=7)
        users = staff_for_period(db, bid, start)
        data = wsvc.week_data(db, bid, start, users)
        act = [r for r in data["rows"].values() if r["accrued"] > 0 and r["state"] != "paid"]
        if ctx.can("payroll.check") and not ctx.can("payroll.edit"):
            _add(out, "/payroll", "yellow", sum(1 for r in act if r["pst"] == "open"), "проверить",
                 f"/payroll/weeks?d={start.isoformat()}")
        if ctx.can("payroll.edit"):
            _add(out, "/payroll", "yellow", sum(1 for r in act if r["pst"] in ("open", "checked")), "утвердить",
                 f"/payroll/weeks?d={start.isoformat()}")
        if ctx.can("payroll.pay"):
            _add(out, "/payroll", "green", sum(1 for r in act if r["pst"] == "approved"), "выдать",
                 f"/payroll/weeks?d={start.isoformat()}")
    return out
