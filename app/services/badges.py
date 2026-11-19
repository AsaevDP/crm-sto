"""Цветные цифры в меню (12.11.2026): у каждого пункта — сколько ждёт именно этого сотрудника.

Цвета (18.11.2026, брат — docs/indicators.md): red — запрос к вам, без вас дело стоит; orange — нужно ваше действие,
но не горит (или подготовиться заранее); green — готово, можно забирать / выдавать; purple — склад ниже нормы.
Просто «для сведения» — цифры нет.
Несколько видов в одном пункте — цифры рядом («🟡 2 🟢 1»). Опрос — /api/notify раз в 15 секунд.
У каждой цифры своя ссылка — нажали, открылся раздел сразу с нужным фильтром.

Наряды: админ, директор, зам — все; остальные — свои (менеджер ведёт / слесарь / запчастист).
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from ..models import IssueRequest, Purchase, PurchaseLine, UsedSale, WoPart, WorkOrder
from ..permissions import MASTERS

ALL_ROLES = ("admin", "director", "deputy")


def _add(out: dict, href: str, color: str, n: int, title: str, link: str = "", alone: bool = False):
    """alone — своя цифра, не складывается с другими того же цвета (штуки запчастей ≠ наряды)."""
    if not n:
        return
    lst = out.setdefault(href, [])
    same = None if alone else next((x for x in lst if x["c"] == color and not x.get("alone")), None)
    if same:   # одного цвета — одной цифрой, подсказка перечисляет всё
        same["n"] += int(n)
        same["t"] += f" · {title}"
    else:
        lst.append({"c": color, "n": int(n), "t": title, "href": link or href, "alone": alone})
    lst.sort(key=lambda x: ("red", "orange", "purple", "green").index(x["c"]))   # purple — «Всегда в наличии» (17.11.2026)


def compute(ctx) -> dict:
    db, me, bid = ctx.db, ctx.user, ctx.branch.id
    role = me.role.code
    out: dict = {}
    see_all = role in ALL_ROLES

    # ---------- Слесарь просит запчасть (11.11.2026) — кладовщику «для сведения»; менеджеру цифра — у «Сообщений» ----------
    if ctx.can("stock.issue"):
        from ..models import OrderMessage
        _add(out, "/stock/issues", "orange", db.scalar(select(func.count()).select_from(OrderMessage).where(
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
            from ..models import OrderMaster   # 13.11.2026: + добавленный мастер, пока не закончил свою диагностику
            n_extra = db.scalar(select(func.count()).select_from(OrderMaster).join(WorkOrder, WorkOrder.id == OrderMaster.work_order_id)
                                .where(WorkOrder.branch_id == bid, WorkOrder.status == "diag", OrderMaster.user_id == me.id,
                                       OrderMaster.active.is_(True), OrderMaster.diag_done_at.is_(None),
                                       WorkOrder.mechanic_id != me.id)) or 0
            n_lead = db.scalar(select(func.count()).select_from(WorkOrder).where(
                WorkOrder.branch_id == bid, WorkOrder.mechanic_id == me.id,
                or_(WorkOrder.status == "created", (WorkOrder.status == "diag") & WorkOrder.lead_diag_at.is_(None)))) or 0
            _add(out, "/orders", "red", n_lead + n_extra, "новые — посмотреть", "/orders?tab=intake")
        # 🔴 13.11.2026: мне передали наряд (менеджеру, мастеру, запчастисту) — горит, пока не нажал «✓ Принял»
        from .handover import pending_orders
        # 19.11.2026: у мойщика «Наряды» = очередь мойки — там блок «Вам передали»
        _add(out, "/orders", "red", len(pending_orders(db, bid, me.id)), "вам передали наряд — нажмите «Принял»",
             "/wash/queue#handed" if role in ("washer", "detailer") else "/orders?tab=handed")
        if ctx.can("orders.approve"):
            _add(out, "/orders", "orange", n_status("approve", field=None if see_all else WorkOrder.manager_id),
                 "согласовать с клиентом", "/orders?tab=approve")
        if ctx.can("parts.pick") and not ctx.can("orders.approve"):
            # 13.11.2026: подбор идёт уже на согласовании — пока не нажал «Подбор завершён»
            qp = select(func.count()).select_from(WorkOrder).where(
                WorkOrder.branch_id == bid, or_(WorkOrder.status == "picking",
                                                (WorkOrder.status == "approve") & WorkOrder.picked_at.is_(None)))
            if not see_all:
                qp = qp.where(WorkOrder.partsman_id == me.id)
            _add(out, "/orders", "orange", db.scalar(qp) or 0, "подобрать запчасти", "/orders?tab=approve")
        if ctx.can("orders.approve"):
            _add(out, "/orders", "green", n_status("ready", field=None if see_all else WorkOrder.manager_id),
                 "готовы к выдаче клиенту", "/orders?tab=ready")
        # 🟢 12.11.2026: пришли запчасти по закупке и ждут выдачи — менеджеру, запчастисту и всем мастерам наряда.
        # Цифра = сколько запчастей. Гаснет, когда кладовщик выдал мастеру. Отдельной цифрой — не смешивается с нарядами.
        arrived = {}
        if ctx.can("orders.approve", "parts.pick") or role in MASTERS:
            from .notify import arrived_waiting
            arrived = arrived_waiting(db, bid, me.id)
            _add(out, "/orders", "green", sum(arrived.values()), "пришли запчасти — ждут выдачи", "/orders?tab=arrived",
                 alone=True)
        if role in ("washer", "detailer"):   # 19.11.2026: мойщику — по очереди мойки: моя машина сейчас / следующая моя
            from . import wash as wash_svc
            brd_ = wash_svc.board(db, bid)
            n_w = sum(1 for c in brd_["doing"] if wash_svc.busy_with(c, me))
            n_w += 1 if wash_svc.next_for(db, bid, me, brd_) else 0
            _add(out, "/orders", "orange", n_w, "ваша машина — можно мыть", "/wash/queue")
        elif role in MASTERS:   # 🟡 12.11.2026: мои работы, которые можно делать прямо сейчас (и у второго мастера тоже)
            from ..models import WoWork
            from .orders import can_start_work
            n_w = 0
            for w, wo_ in db.execute(select(WoWork, WorkOrder).join(WorkOrder, WorkOrder.id == WoWork.work_order_id).where(
                    WorkOrder.branch_id == bid, WoWork.mechanic_id == me.id, WoWork.status != "done",
                    WorkOrder.status.in_(("picking", "waiting", "instock", "work")))):
                if can_start_work(db, wo_, w):
                    n_w += 1
            _add(out, "/orders", "orange", n_w, "ваши работы — можно делать", "/orders?tab=active")
        if role in MASTERS:   # «можно начинать» — только наряды, где ничего не заказывали (пришедшее уже посчитано выше)
            qi = select(func.count()).select_from(WorkOrder).where(
                WorkOrder.branch_id == bid, WorkOrder.status == "instock", WorkOrder.mechanic_id == me.id)
            if arrived:
                qi = qi.where(WorkOrder.id.not_in(list(arrived)))
            n_in = db.scalar(qi) or 0
            _add(out, "/orders", "green", n_in, "запчасти на складе — можно начинать", "/orders?tab=parts")

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

    # ---------- Мои запросы на выдачу (мастер, 12.11.2026) ----------
    if role in MASTERS:
        from ..models import ChemRequest, OrderMessage, ToolRequest, WoWork
        my_wo = select(WorkOrder.id).where(WorkOrder.branch_id == bid, or_(
            WorkOrder.mechanic_id == me.id, WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me.id))))
        mine_req = or_(IssueRequest.created_by == me.id, IssueRequest.work_order_id.in_(my_wo))
        _add(out, "/my-requests", "green", db.scalar(select(func.count()).select_from(IssueRequest).where(
            IssueRequest.branch_id == bid, IssueRequest.status == "ready", mine_req)) or 0,
             "готово — забрать у окошка кладовщика", "/my-requests?tab=parts")
        wait = (db.scalar(select(func.count()).select_from(IssueRequest).where(
                    IssueRequest.branch_id == bid, IssueRequest.status == "new", mine_req)) or 0) \
            + (db.scalar(select(func.count()).select_from(OrderMessage).where(
                OrderMessage.branch_id == bid, OrderMessage.kind == "part", OrderMessage.status == "pending",
                OrderMessage.created_by == me.id)) or 0)
        _add(out, "/my-requests", "orange", wait, "ждут кладовщика или менеджера", "/my-requests?tab=parts")
        tq = select(func.count()).select_from(ToolRequest).where(ToolRequest.branch_id == bid, ToolRequest.user_id == me.id)
        cq = select(func.count()).select_from(ChemRequest).where(ChemRequest.branch_id == bid, ChemRequest.created_by == me.id)
        # 🟢 19.11.2026 (брат): кладовщик подготовил инструмент / химию — «готово, подойдите», как с запчастями
        _add(out, "/my-requests", "green", db.scalar(tq.where(ToolRequest.status == "ready")) or 0,
             "инструмент готов — забрать у кладовщика", "/my-requests?tab=tools")
        _add(out, "/my-requests", "green", db.scalar(cq.where(ChemRequest.status == "ready")) or 0,
             "химия готова — забрать у кладовщика", "/my-requests?tab=chem")
        _add(out, "/my-requests", "orange", db.scalar(tq.where(ToolRequest.status == "new")) or 0,
             "инструмент — ждёт кладовщика", "/my-requests?tab=tools")
        _add(out, "/my-requests", "orange", db.scalar(cq.where(ChemRequest.status == "new")) or 0,
             "химия — ждёт кладовщика", "/my-requests?tab=chem")

    # ---------- Запросы на выдачу (кладовщик) ----------
    if ctx.can("stock.issue", "tools.manage"):
        if ctx.can("stock.issue"):
            _add(out, "/stock/issues", "red", db.scalar(select(func.count()).select_from(IssueRequest).where(
                IssueRequest.branch_id == bid, IssueRequest.status == "new")) or 0, "просят выдать запчасти")
            from ..models import ChemRequest   # 12.11.2026: мойщик / детейлер просит химию
            _add(out, "/stock/issues", "red", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "new")) or 0, "просят химию для мойки", "/stock/issues?tab=chem")
        if ctx.can("tools.manage"):
            # 18.11.2026 (брат): инструмент — цифра у «Инструмента» (раньше пряталась в «Запросах на выдачу»).
            # Ссылка — та же вкладка «Инструмент» в выдаче, где кнопки «Выдать» / «Принял».
            from ..models import ToolRequest
            tq = select(func.count()).select_from(ToolRequest).where(ToolRequest.branch_id == bid, ToolRequest.status == "new")
            _add(out, "/tools", "red", db.scalar(tq.where(ToolRequest.kind == "issue")) or 0, "просят инструмент — выдать",
                 "/stock/issues?tab=tools")
            _add(out, "/tools", "orange", db.scalar(tq.where(ToolRequest.kind == "return")) or 0, "сдают инструмент — принять",
                 "/stock/issues?tab=tools")
        # «готово — ждут, когда заберут» — от кладовщика действия не нужно, цифру убрали (18.11.2026)

    # ---------- Мойка (18.11.2026, брат) ----------
    if ctx.can("wash.view"):
        from ..models import ChemRequest
        wq = select(func.count()).select_from(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.kind == "wash",
                                                               WorkOrder.status.in_(("ready", "paid")))
        if not see_all:
            wq = wq.where(WorkOrder.manager_id == me.id)
        _add(out, "/wash", "green", db.scalar(wq) or 0, "помыты — позвонить клиенту, оплата и выдача", "/wash?tab=now")
        if not ctx.can("stock.issue"):   # у кладовщика — у «Запросов на выдачу»
            _add(out, "/wash", "orange", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "new")) or 0, "мойщик просит химию — ждёт кладовщика",
                 "/wash?tab=asks")
        # 🔴 19.11.2026: мойщик просит изменить очередь — ждёт ответа менеджера
        if ctx.can("orders.approve"):
            from ..models import WashQueueRequest
            if db.scalar(select(WashQueueRequest.id).where(WashQueueRequest.branch_id == bid,
                                                           WashQueueRequest.status == "new").limit(1)):
                from . import wash as wash_svc
                _add(out, "/wash", "red", len(wash_svc.open_asks(db, bid)), "мойщик просит изменить очередь",
                     "/wash?tab=asksq")
    if role in ("washer", "detailer"):   # машины в очереди мойки, которые никто не взял (и мыть уже можно)
        from . import wash as wash_svc
        brd = wash_svc.board(db, bid)
        _add(out, "/orders", "red", sum(1 for c in brd["queue"] if not c.who and not c.blocked), "в очереди — никто не взял",
             "/wash/queue")

    # ---------- Склад, Закупки (кладовщик) ----------
    if ctx.can("stock.receive") and role != "content":   # кладовщик разбора обычные поставки не принимает
        # 18.11.2026: ждём приход — оранжевым (подготовиться принять); опаздывают (прошла дата «привезут») — красным
        wait_q = (select(func.count()).select_from(PurchaseLine).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                  .where(Purchase.branch_id == bid, Purchase.status.in_(("ordered", "partial")), PurchaseLine.received.is_(False)))
        late = db.scalar(wait_q.where(Purchase.expected_on.is_not(None), Purchase.expected_on < date.today())) or 0
        _add(out, "/stock", "red", late, "опаздывают — позвонить поставщику", "/stock/receive?tab=late")
        _add(out, "/stock", "orange", (db.scalar(wait_q) or 0) - late, "ждём приход — подготовиться принять", "/stock/receive")
    # 17.11.2026 (брат): «Всегда в наличии» — сколько позиций ниже нормы, сиреневая цифра у «Склада»
    if ctx.can("stock.keep"):
        from . import keep as ksvc
        _add(out, "/stock", "purple", ksvc.below_count(db, bid), "«Всегда в наличии» — ниже нормы", "/stock/always")
    # 17.11.2026 (брат): в «Закупках» у кладовщика — только свои заказы на склад; цифра — его черновики («Заказал» не нажат)
    if ctx.can("purchases.stock") and not ctx.can("purchases.manage"):
        _add(out, "/purchases", "orange", db.scalar(select(func.count()).select_from(Purchase).where(
            Purchase.branch_id == bid, Purchase.status == "draft", Purchase.created_by == me.id)) or 0,
             "ваши черновики — нажмите «Заказан»", "/purchases?tab=draft")

    # ---------- 19.11.2026 (брат): клиент отказался после согласования — «Лишнее после отказов» ----------
    if ctx.can("parts.pick", "stock.manage"):
        from ..models import PartLeftover
        lq = select(func.count()).select_from(PartLeftover).where(PartLeftover.branch_id == bid)
        mine_href = "/my-orders" if ctx.can("purchases.mine") and not ctx.can("purchases.by_partsman") else "/stock"
        if ctx.can("parts.pick"):   # 🔴 заказано у поставщика — отменить или пусть приедет (решает запчастист)
            _add(out, mine_href, "red", db.scalar(lq.where(PartLeftover.status == "decide")) or 0,
                 "клиент отказался — решите с заказом у поставщика", "/stock/leftovers")
        _add(out, mine_href, "orange", db.scalar(lq.where(PartLeftover.status == "stock")) or 0,
             "лишнее после отказа на складе — оставить, вернуть или продать", "/stock/leftovers")

    # ---------- Мои заказы (запчастист) ----------
    if ctx.can("purchases.mine") and not ctx.can("purchases.by_partsman"):
        from . import stock as ssvc
        rows = ssvc.partsman_lines(db, bid, me.id, days=1)
        _add(out, "/my-orders", "red", sum(1 for r in rows if r["late"]), "опаздывают", "/my-orders?st=late")
        # 12.11.2026: черновик под наряд не отправлен поставщику — в наряде это «в черновике», кладовщик ждёт
        _add(out, "/my-orders", "orange", len(ssvc.draft_waits(db, bid, me.id)), "черновики под наряды — нажмите «✓ Заказал»",
             "/my-orders?st=draft")
        # 18.11.2026 (брат): «приняты на склад сегодня» — только для сведения, цифру убрали

    # ---------- Разбор: выдача и отправка ----------
    if ctx.can("razbor.manage"):
        base = select(func.count()).select_from(UsedSale).where(UsedSale.branch_id == bid, UsedSale.status == "reserved")
        _add(out, "/razbor/shipping", "red", db.scalar(base.where(UsedSale.packed_at.is_(None))) or 0, "подготовить",
             "/razbor/shipping?st=prepare")
        _add(out, "/razbor/shipping", "orange", db.scalar(base.where(UsedSale.packed_at.is_not(None))) or 0,
             "готово — ждёт покупателя или отправки", "/razbor/shipping?st=ready_ship")

    # ---------- Цены разбора (18.11.2026, брат) ----------
    if ctx.can("razbor.price") or ctx.can("razbor.manage"):
        from . import razbor_price as rp
        rc = rp.counts(db, bid)
        if ctx.can("razbor.price"):
            _add(out, "/razbor/prices", "red", rc["new"], "новые детали — утвердить цену", "/razbor/prices?tab=new")
            _add(out, "/razbor/prices", "orange", rc["change"], "просят изменить цену", "/razbor/prices?tab=change")
        if ctx.can("razbor.manage"):
            _add(out, "/razbor", "red", rc["declined"], "отказано в утверждении — посмотреть причину", "/razbor?f=declined")

    # ---------- Зарплата: прошлая неделя ждёт проверки / утверждения ----------
    if ctx.can("payroll.check", "payroll.edit"):
        from . import payweek as wsvc
        from .staff import staff_for_period
        start = wsvc.monday(date.today()) - timedelta(days=7)
        users = staff_for_period(db, bid, start)
        data = wsvc.week_data(db, bid, start, users)
        act = [r for r in data["rows"].values() if r["accrued"] > 0 and r["state"] != "paid"]
        if ctx.can("payroll.check") and not ctx.can("payroll.edit"):
            _add(out, "/payroll", "orange", sum(1 for r in act if r["pst"] == "open"), "проверить",
                 f"/payroll/weeks?d={start.isoformat()}")
        if ctx.can("payroll.edit"):
            _add(out, "/payroll", "orange", sum(1 for r in act if r["pst"] in ("open", "checked")), "утвердить",
                 f"/payroll/weeks?d={start.isoformat()}")
        if ctx.can("payroll.pay"):
            _add(out, "/payroll", "green", sum(1 for r in act if r["pst"] == "approved"), "выдать",
                 f"/payroll/weeks?d={start.isoformat()}")
    return out
