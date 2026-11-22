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


def _add(out: dict, href: str, color: str, n: int, title: str, link: str = "", alone: bool = False, info: bool = False,
         wo=None, blk: str = "", rows=None, sel=None, tile=None):
    """alone — своя цифра, не складывается с другими того же цвета (штуки запчастей ≠ наряды).
    info — 19.11.2026 (брат): «для сведения» (вы следите, делает другой) — цифра с сиреневой обводкой; с цифрами
    «действие ваше» того же цвета не складывается.
    22.11.2026 (брат): кружок ведёт прямо к делу и обводит его бегущей рамкой своего цвета, пока дело не сделано
    (app.js: crmNeed). wo — наряды, где дело (один — ссылка сразу в наряд на блок blk; несколько — список, в нём
    обведён каждый наряд); rows — строки внутри наряда (селекторы, например [data-id="…"]); sel — [(адрес, селектор)]
    для дел не в наряде."""
    if not n:
        return
    wo = [str(i) for i in dict.fromkeys(wo or [])]
    if len(wo) == 1 and blk:
        link = f"/orders/{wo[0]}{blk}"
    lst = out.setdefault(href, [])
    # 20.11.2026 (брат): в один кружок сливаются только дела, которые открываются одним и тем же экраном. Разные
    # экраны — разные кружки (раньше «новые» и «вам передали» были одной цифрой, клик вёл только к первому)
    same = None if alone else next((x for x in lst if x["c"] == color and not x.get("alone")
                                    and bool(x.get("info")) == info and x["href"] == (link or href)), None)
    if same:   # одного цвета — одной цифрой, подсказка перечисляет всё
        same["n"] += int(n)
        same["t"] += f" · {title}"
        _merge_marks(same, {"wo": wo, "blk": blk, "rows": rows or [], "sel": sel or [], "tile": list(tile or [])})
    else:
        lst.append({"c": color, "n": int(n), "t": title + (" (для сведения)" if info else ""), "href": link or href,
                    "alone": alone, "info": info, "wo": wo, "blk": blk, "rows": list(rows or []),
                    "sel": [list(s) for s in (sel or [])], "tile": list(tile or [])})
    lst.sort(key=lambda x: ("red", "orange", "purple", "green").index(x["c"]))   # purple — «Всегда в наличии» (17.11.2026)


def _merge_marks(a: dict, b: dict) -> None:
    """Слили два кружка в один — и места для рамки тоже."""
    a["wo"] = list(dict.fromkeys(a.get("wo", []) + b.get("wo", [])))
    a["blk"] = a.get("blk") or b.get("blk", "")
    a["rows"] = a.get("rows", []) + b.get("rows", [])
    a["sel"] = a.get("sel", []) + [list(s) for s in b.get("sel", [])]
    a["tile"] = list(dict.fromkeys(a.get("tile", []) + b.get("tile", [])))
    a.setdefault("also", []).append(b.get("href", ""))


MAX_CIRCLES = 3   # 20.11.2026: больше трёх кружков у пункта меню не помещается — подпись переносится


def _compact(out: dict) -> dict:
    """Кружков у пункта больше трёх — одного цвета снова сливаем в один (ведёт к первому делу; на самом экране
    дела и так разложены по цветным плиткам)."""
    for href, lst in out.items():
        # по одному цвету за раз — сначала менее срочные (зелёный, сиреневый, оранжевый), красные — в последнюю очередь
        for color in ("green", "purple", "orange", "red"):
            if len(lst) <= MAX_CIRCLES:
                break
            merged = []
            for x in lst:
                same = next((m for m in merged if x["c"] == color and m["c"] == color
                             and bool(m.get("info")) == bool(x.get("info"))
                             and not m.get("alone") and not x.get("alone")), None)
                if same:
                    same["n"] += x["n"]
                    same["t"] += f" · {x['t']}"
                    _merge_marks(same, x)
                else:
                    merged.append(dict(x))
            lst = merged
        out[href] = lst
    return out


def _zone_keeper_on_site(db, bid, role_code: str) -> bool:
    """Кладовщик этой зоны сейчас на работе (по табелю: последняя отметка за сегодня — «пришёл»)."""
    from ..models import Role, User
    from .attendance import on_site_now
    people = db.scalars(select(User).join(Role, Role.id == User.role_id).where(
        User.branch_id == bid, User.active.is_(True), Role.code == role_code)).all()
    if not people:
        return False
    last = on_site_now(db, bid, people)
    return any(last.get(u.id) is not None and last[u.id].direction == "in" for u in people)


def compute(ctx) -> dict:
    db, me, bid = ctx.db, ctx.user, ctx.branch.id
    role = me.role.code
    out: dict = {}
    see_all = role in ALL_ROLES
    # 20.11.2026 (брат): главный кладовщик — обе зоны склада. Кладовщик зоны сегодня на работе (по табелю) — у главного
    # цифры этой зоны «для сведения» (сиреневая обводка); его нет (не пришёл, ушёл, должности нет) — обычные, подменяет
    info_new = role == "headkeeper" and _zone_keeper_on_site(db, bid, "storekeeper")
    info_rz = role == "headkeeper" and _zone_keeper_on_site(db, bid, "content")

    # ---------- Слесарь просит запчасть (11.11.2026) — кладовщику «для сведения»; менеджеру цифра — у «Сообщений» ----------
    if ctx.can("stock.issue", "razbor.issue"):
        from ..models import OrderMessage, PartCatalog
        pq = select(func.count()).select_from(OrderMessage).join(PartCatalog, PartCatalog.id == OrderMessage.catalog_id).where(
            OrderMessage.branch_id == bid, OrderMessage.kind == "part", OrderMessage.status == "pending")
        if ctx.can("stock.issue"):   # 20.11.2026: по зонам — новые у кладовщика, б/у у кладовщика разбора
            _add(out, "/stock/issues", "orange", db.scalar(pq.where(PartCatalog.kind != "used")) or 0,
                 "слесарь просит — ждёт согласования менеджера", info=info_new, tile=['Запчасти'])
        if ctx.can("razbor.issue"):
            _add(out, "/stock/issues", "orange", db.scalar(pq.where(PartCatalog.kind == "used")) or 0,
                 "слесарь просит б/у — ждёт согласования менеджера", "/stock/issues?tab=razbor", info=info_rz, tile=['Б/у · разбор'])

    # ---------- Наряды ----------
    if ctx.can("orders.view_all", "orders.view_own"):
        mine = or_(WorkOrder.manager_id == me.id, WorkOrder.mechanic_id == me.id, WorkOrder.partsman_id == me.id)

        def ids_status(*sts, field=None):
            """22.11.2026: не число, а сами наряды — чтобы обвести каждый."""
            qq = select(WorkOrder.id).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(sts))
            if field is not None:
                qq = qq.where(field == me.id)
            elif not see_all:
                qq = qq.where(mine)
            return db.scalars(qq).all()

        # 22.11.2026 (брат): «🔥 Срочно» у детали — цифра только у того, от кого зависит дело.
        # Запчастист — 🔴 «срочная деталь не пришла» (ещё не на складе: подбор / заказана), гаснет, когда её примут.
        # Менеджер — 🔴 только «срочная деталь без срока»: нет даты «привезут» — спросить у запчастиста; дата появилась —
        # цифра гаснет (пока деталь едет, у менеджера цифры нет; действия кладовщика на его цифры не влияют).
        if ctx.can("orders.approve", "parts.pick"):
            pq0 = (select(WoPart.id, WoPart.work_order_id).join(WorkOrder, WorkOrder.id == WoPart.work_order_id)
                   .where(WorkOrder.branch_id == bid, WoPart.urgent.is_(True), WoPart.status.in_(("need", "ordered")),
                          WorkOrder.status.not_in(("closed", "cancel"))))

            def urgent_badge(q, title):
                rows_ = db.execute(q).all()   # один наряд — цифра ведёт прямо к его запчастям, деталь обведена
                ids = list(dict.fromkeys(r[1] for r in rows_))
                _add(out, "/orders", "red", len(ids), title, "/orders?tab=parts", wo=ids, blk="#parts",
                     rows=[f'.part[data-id="{r[0]}"]' for r in rows_], tile=["Запчасти"])
            if ctx.can("parts.pick"):
                urgent_badge(pq0 if see_all else pq0.where(WorkOrder.partsman_id == me.id),
                             "срочная деталь не пришла — поторопите поставщика")
            if ctx.can("orders.approve"):
                dated = (select(PurchaseLine.id).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                         .where(PurchaseLine.wo_part_id == WoPart.id, Purchase.expected_on.is_not(None)).exists())
                mq = pq0.where(~dated)
                urgent_badge(mq if see_all else mq.where(WorkOrder.manager_id == me.id),
                             "срочная деталь без срока — спросите у запчастиста")
        if role in MASTERS:
            from ..models import OrderMaster   # 13.11.2026: + добавленный мастер, пока не закончил свою диагностику
            ids_extra = db.scalars(select(WorkOrder.id).join(OrderMaster, WorkOrder.id == OrderMaster.work_order_id)
                                   .where(WorkOrder.branch_id == bid, WorkOrder.status == "diag", OrderMaster.user_id == me.id,
                                          OrderMaster.active.is_(True), OrderMaster.diag_done_at.is_(None),
                                          WorkOrder.mechanic_id != me.id)).all()
            ids_lead = db.scalars(select(WorkOrder.id).where(
                WorkOrder.branch_id == bid, WorkOrder.mechanic_id == me.id,
                or_(WorkOrder.status == "created", (WorkOrder.status == "diag") & WorkOrder.lead_diag_at.is_(None)))).all()
            _add(out, "/orders", "red", len(ids_lead) + len(ids_extra), "новые — посмотреть", "/orders?tab=intake",
                 wo=[*ids_lead, *ids_extra], blk="#problems", rows=["#btn-diag-done", "#btn-add-finding"], tile=['Приём и диагностика'])
        # 🔴 13.11.2026: мне передали наряд (менеджеру, мастеру, запчастисту) — горит, пока не нажал «✓ Принял»
        from .handover import pending_orders
        # 19.11.2026: у мойщика «Наряды» = очередь мойки — там блок «Вам передали»
        handed = pending_orders(db, bid, me.id)
        washer_ = role in ("washer", "detailer")
        _add(out, "/orders", "red", len(handed), "вам передали наряд — нажмите «Принял»",
             "/wash/queue#handed" if washer_ else "/orders?tab=handed",
             wo=[] if washer_ else handed, blk="#ho-accept", tile=[] if washer_ else ['Мне передали'])   # у мойщика плиток нет — блок «Вам передали»
        if ctx.can("orders.approve"):
            ids_ = ids_status("approve", field=None if see_all else WorkOrder.manager_id)
            _add(out, "/orders", "orange", len(ids_), "согласовать с клиентом", "/orders?tab=approve", wo=ids_,
                 blk="#problems", rows=["#btn-approve-done"], tile=['Согласование'])
        if ctx.can("parts.pick") and not ctx.can("orders.approve"):
            # 13.11.2026: подбор идёт уже на согласовании — пока не нажал «Подбор завершён»
            qp = select(WorkOrder.id).where(
                WorkOrder.branch_id == bid, or_(WorkOrder.status == "picking",
                                                (WorkOrder.status == "approve") & WorkOrder.picked_at.is_(None)))
            if not see_all:
                qp = qp.where(WorkOrder.partsman_id == me.id)
            ids_ = db.scalars(qp).all()
            _add(out, "/orders", "orange", len(ids_), "подобрать запчасти", "/orders?tab=approve", wo=ids_, blk="#parts",
                 rows=["#btn-picked"], tile=['Согласование'])
        if ctx.can("orders.approve"):
            ids_ = ids_status("ready", field=None if see_all else WorkOrder.manager_id)
            _add(out, "/orders", "green", len(ids_), "готовы к выдаче клиенту", "/orders?tab=ready", wo=ids_, blk="#money",
                 rows=["#btn-pay", "#btn-handover"], tile=['Готовы к выдаче'])
        # 🟢 12.11.2026: пришли запчасти по закупке и ждут выдачи — менеджеру, запчастисту и всем мастерам наряда.
        # Цифра = сколько запчастей. Гаснет, когда кладовщик выдал мастеру. Отдельной цифрой — не смешивается с нарядами.
        arrived = {}
        # 20.11.2026 (брат): мойщику и детейлеру запчасти не нужны — этих цифр у них нет
        if ctx.can("orders.approve", "parts.pick") or (role in MASTERS and role not in ("washer", "detailer")):
            from .notify import arrived_waiting
            arrived = arrived_waiting(db, bid, me.id)
            # 22.11.2026 (брат): в наряде мигает край у каждой пришедшей детали
            arr_rows = db.scalars(select(WoPart.id).join(PurchaseLine, PurchaseLine.wo_part_id == WoPart.id).where(
                WoPart.work_order_id.in_(list(arrived) or [None]), WoPart.status == "reserved",
                or_(PurchaseLine.received.is_(True), PurchaseLine.received_qty > 0))).all() if arrived else []
            _add(out, "/orders", "green", sum(arrived.values()), "пришли запчасти — ждут выдачи", "/orders?tab=arrived",
                 alone=True, wo=list(arrived), blk="#parts", rows=[f'.part[data-id="{i}"]' for i in arr_rows] + ["#btn-request-issue"], tile=['Пришли запчасти'])
        if role in ("washer", "detailer"):   # 19.11.2026: мойщику — по очереди мойки: моя машина сейчас / следующая моя
            from . import wash as wash_svc
            brd_ = wash_svc.board(db, bid)
            # 20.11.2026 (брат): клик ведёт прямо к своему блоку очереди — «Я мою сейчас» / «Следующая»
            now_ = [c.o.id for c in brd_["doing"] if wash_svc.busy_with(c, me)]
            _add(out, "/orders", "orange", len(now_), "я мою сейчас — отметить «Выполнено»", "/wash/queue#now", wo=now_)
            # 20.11.2026 (брат): одна цифра — все машины, которые он может мыть сейчас (ничьи + записанные на него
            # с ремонта); раньше «следующая» и «никто не взял» считали одну машину дважды, а ремонтные — нигде
            # 22.11.2026: в очереди обведена каждая такая машина
            wait_ = [c.o.id for c in wash_svc.waiting_for(db, bid, me, brd_)]
            _add(out, "/orders", "orange", len(wait_), "ждут мойки — ваши (можно мыть)", "/wash/queue#next", wo=wait_)
        elif role in MASTERS:   # 🟡 12.11.2026: мои работы, которые можно делать прямо сейчас (и у второго мастера тоже)
            from ..models import WoWork
            from .orders import can_start_work
            from .receipt import start_blocked
            ok_works = []
            for w, wo_ in db.execute(select(WoWork, WorkOrder).join(WorkOrder, WorkOrder.id == WoWork.work_order_id).where(
                    WorkOrder.branch_id == bid, WoWork.mechanic_id == me.id, WoWork.status != "done",
                    WorkOrder.status.in_(("picking", "waiting", "instock", "work")))):
                if can_start_work(db, wo_, w) and not start_blocked(db, wo_, w):   # 20.11: сначала «Получил»
                    ok_works.append(w)
            _add(out, "/orders", "orange", len(ok_works), "ваши работы — можно делать", "/orders?tab=active",
                 wo=[w.work_order_id for w in ok_works], blk="#works", rows=[f'.work[data-id="{w.id}"]' for w in ok_works], tile=['На СТО'])
        if role in MASTERS and role not in ("washer", "detailer"):   # «можно начинать» — только наряды, где ничего не заказывали (пришедшее уже посчитано выше)
            qi = select(WorkOrder.id).where(
                WorkOrder.branch_id == bid, WorkOrder.status == "instock", WorkOrder.mechanic_id == me.id)
            if arrived:
                qi = qi.where(WorkOrder.id.not_in(list(arrived)))
            ids_ = db.scalars(qi).all()
            _add(out, "/orders", "green", len(ids_), "запчасти на складе — можно начинать", "/orders?tab=parts",
                 wo=ids_, blk="#parts", rows=["#btn-request-issue"], tile=['Запчасти'])

    # 🔴 менеджер просит доступ ко всем нарядам — админу / директору / заму
    if ctx.can("orders.grant_access"):
        from ..models import OrderAccess
        _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(OrderAccess).where(
            OrderAccess.branch_id == bid, OrderAccess.status == "pending")) or 0, "просят доступ ко всем нарядам",
             "/access-requests#all", sel=[("/access-requests", ".ac-card.now")], tile=['Ко всем нарядам'])
        from ..models import ClientAccess   # 12.11.2026: менеджер просит доступ к клиенту
        # 20.11.2026 (брат): у «Клиентов» цифра = плитке «Просят доступ»: к одному клиенту + ко всем нарядам (вся база)
        _add(out, "/clients", "red", db.scalar(select(func.count()).select_from(ClientAccess).where(
            ClientAccess.branch_id == bid, ClientAccess.status == "pending")) or 0, "менеджер просит доступ к клиенту",
             "/access-requests#clients", sel=[("/access-requests", ".ac-card.now")], tile=['К клиенту'])
        _add(out, "/clients", "red", db.scalar(select(func.count()).select_from(OrderAccess).where(
            OrderAccess.branch_id == bid, OrderAccess.status == "pending")) or 0,
             "просят доступ ко всем нарядам и клиентам", "/access-requests#all", sel=[("/access-requests", ".ac-card.now")], tile=['Ко всем нарядам'])
        from ..models import OrderReopen   # 11.11.2026: просят открыть закрытый наряд заново
        _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(OrderReopen).where(
            OrderReopen.branch_id == bid, OrderReopen.status == "pending")) or 0, "просят открыть наряд заново",
             "/access-requests#reopen", sel=[("/access-requests", ".ac-card.now")], tile=['Открыть заново'])

    # ---------- Готово к выдаче (менеджер) ----------
    if ctx.can("stock.pickup") and not ctx.can("stock.issue"):
        n = 0
        for r, wo in db.execute(select(IssueRequest, WorkOrder).join(WorkOrder, WorkOrder.id == IssueRequest.work_order_id)
                                .where(IssueRequest.branch_id == bid, IssueRequest.status == "ready")):
            if wo.manager_id == me.id or r.created_by == me.id:
                n += 1
        _add(out, "/ready", "green", n, "кладовщик подготовил", "/ready#mine", tile=['Забрать у окошка'])
        # 🔴 20.11.2026 (брат, наряд 60): мастер просит со склада под мой наряд — согласовать / отказать
        from . import messages as msg_svc
        _add(out, "/ready", "red", len(msg_svc.my_pending(ctx)), "мастер просит со склада — согласуйте", "/ready#asks", tile=['Согласовать'])
        # 19.11.2026 (брат, наряд 60): по моим нарядам мастер попросил у кладовщика — менеджер видит и запчасти, и
        # инструмент: 🟠 ждёт кладовщика (проследить), 🟢 инструмент подготовлен — мастер заберёт
        from ..models import ToolRequest
        _add(out, "/ready", "orange", db.scalar(select(func.count()).select_from(IssueRequest).join(
            WorkOrder, WorkOrder.id == IssueRequest.work_order_id).where(
            IssueRequest.branch_id == bid, IssueRequest.status == "new", WorkOrder.manager_id == me.id)) or 0,
             "запчасти — ждут кладовщика", "/ready#waiting", tile=['Ждут кладовщика'])
        tq = select(func.count()).select_from(ToolRequest).join(WorkOrder, WorkOrder.id == ToolRequest.work_order_id).where(
            ToolRequest.branch_id == bid, ToolRequest.kind == "issue", WorkOrder.manager_id == me.id)
        # 19.11.2026 (брат): инструмент — как запчасти, только у «Запросов на выдачу» (там же «Выдано»);
        # у «Инструмента» цифр нет (сиреневую «для сведения» убрали)
        _add(out, "/ready", "orange", db.scalar(tq.where(ToolRequest.status == "new")) or 0,
             "инструмент — ждёт кладовщика", "/ready#waiting", tile=['Ждут кладовщика', 'Инструмент'])
        _add(out, "/ready", "green", db.scalar(tq.where(ToolRequest.status == "ready")) or 0,
             "инструмент подготовлен — забрать у окошка и отнести мастеру", "/ready#tools", tile=['Забрать у окошка'])

    # ---------- Мои запросы на выдачу (мастер, 12.11.2026) ----------
    if role in MASTERS:
        from ..models import ChemRequest, OrderMessage, ToolRequest, WoWork
        my_wo = select(WorkOrder.id).where(WorkOrder.branch_id == bid, or_(
            WorkOrder.mechanic_id == me.id, WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me.id))))
        mine_req = or_(IssueRequest.created_by == me.id, IssueRequest.work_order_id.in_(my_wo))
        if role in ("washer", "detailer"):   # 20.11.2026: запчасти слесарей в машине, которую он моет, — не его
            mine_req = IssueRequest.created_by == me.id
        _add(out, "/my-requests", "green", db.scalar(select(func.count()).select_from(IssueRequest).where(
            IssueRequest.branch_id == bid, IssueRequest.status == "ready", mine_req)) or 0,
             "готово — забрать у окошка кладовщика", "/my-requests?tab=parts", tile=['Запчасти', 'Б/у · разбор'])
        wait = (db.scalar(select(func.count()).select_from(IssueRequest).where(
                    IssueRequest.branch_id == bid, IssueRequest.status == "new", mine_req)) or 0) \
            + (db.scalar(select(func.count()).select_from(OrderMessage).where(
                OrderMessage.branch_id == bid, OrderMessage.kind == "part", OrderMessage.status == "pending",
                OrderMessage.created_by == me.id)) or 0)
        _add(out, "/my-requests", "orange", wait, "ждут кладовщика или менеджера", "/my-requests?tab=parts", tile=['Запчасти', 'Б/у · разбор'])
        tq = select(func.count()).select_from(ToolRequest).where(ToolRequest.branch_id == bid, ToolRequest.user_id == me.id)
        cq = select(func.count()).select_from(ChemRequest).where(ChemRequest.branch_id == bid, ChemRequest.created_by == me.id)
        # 🟢 19.11.2026 (брат): кладовщик подготовил инструмент / химию — «готово, подойдите», как с запчастями
        _add(out, "/my-requests", "green", db.scalar(tq.where(ToolRequest.status == "ready")) or 0,
             "инструмент готов — забрать у кладовщика", "/my-requests?tab=tools", tile=['Инструмент'])
        _add(out, "/my-requests", "green", db.scalar(cq.where(ChemRequest.status == "ready")) or 0,
             "химия готова — забрать у кладовщика", "/my-requests?tab=chem", tile=['Химия'])
        _add(out, "/my-requests", "orange", db.scalar(tq.where(ToolRequest.status == "new")) or 0,
             "инструмент — ждёт кладовщика", "/my-requests?tab=tools", tile=['Ждут кладовщика', 'Инструмент'])
        _add(out, "/my-requests", "orange", db.scalar(cq.where(ChemRequest.status == "new")) or 0,
             "химия — ждёт кладовщика", "/my-requests?tab=chem", tile=['Химия'])
        # 🟢 20.11.2026 (брат): выдали — подтвердите «✓ Получил» (запчасти: до этого «Начал» не нажимается)
        from . import receipt as rsvc
        _add(out, "/my-requests", "green", len(rsvc.for_master(db, bid, me.id)),
             "запчасти выданы — подтвердите «Получил»", "/my-requests?tab=parts", tile=['Запчасти', 'Б/у · разбор'])
        _add(out, "/my-requests", "green", len(rsvc.tools_for_master(db, bid, me.id)),
             "инструмент выдан — подтвердите «Получил»", "/my-requests?tab=tools", tile=['Инструмент'])

    # ---------- Запросы на выдачу (кладовщик) ----------
    if ctx.can("stock.issue", "tools.manage", "razbor.issue"):
        iq = select(func.count()).select_from(IssueRequest).where(IssueRequest.branch_id == bid)
        if ctx.can("stock.issue"):
            # 20.11.2026 (брат): только своя зона — новые запчасти (б/у выдаёт кладовщик разбора)
            _add(out, "/stock/issues", "red", db.scalar(iq.where(IssueRequest.status == "new", IssueRequest.zone == "new")) or 0,
                 "просят выдать запчасти", info=info_new, tile=['Запчасти'])
            from ..models import ChemRequest   # 12.11.2026: мойщик / детейлер просит химию
            _add(out, "/stock/issues", "red", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "new")) or 0, "просят химию для мойки", "/stock/issues?tab=chem",
                 info=info_new, tile=['Химия'])
            # 🟠 19.11.2026 (брат): подготовил — ещё не выдано (горит, пока не нажато «Выдано»)
            _add(out, "/stock/issues", "orange", db.scalar(iq.where(IssueRequest.status == "ready", IssueRequest.zone == "new")) or 0,
                 "запчасти подготовлены — ещё не выданы", "/stock/issues", info=info_new, tile=['Запчасти'])
            _add(out, "/stock/issues", "orange", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "ready")) or 0,
                 "химия подготовлена — ещё не выдана", "/stock/issues?tab=chem", info=info_new, tile=['Химия'])
        if ctx.can("razbor.issue"):
            # 🔴 / 🟠 20.11.2026 (брат): б/у под наряд — у кладовщика разбора своё окошко
            _add(out, "/stock/issues", "red", db.scalar(iq.where(IssueRequest.status == "new", IssueRequest.zone == "razbor")) or 0,
                 "просят выдать б/у с разбора", "/stock/issues?tab=razbor", info=info_rz, tile=['Б/у · разбор'])
            _add(out, "/stock/issues", "orange", db.scalar(iq.where(IssueRequest.status == "ready", IssueRequest.zone == "razbor")) or 0,
                 "б/у подготовлено — ещё не выдано", "/stock/issues?tab=razbor", info=info_rz, tile=['Б/у · разбор'])
        if ctx.can("tools.manage"):
            # 18.11.2026 (брат): инструмент — цифра у «Инструмента» (раньше пряталась в «Запросах на выдачу»).
            # Ссылка — та же вкладка «Инструмент» в выдаче, где кнопки «Выдать» / «Принял».
            from ..models import ToolRequest
            tq = select(func.count()).select_from(ToolRequest).where(ToolRequest.branch_id == bid, ToolRequest.status == "new")
            _add(out, "/tools", "red", db.scalar(tq.where(ToolRequest.kind == "issue")) or 0, "просят инструмент — выдать",
                 "/stock/issues?tab=tools", info=info_new, tile=['Инструмент'])
            _add(out, "/tools", "orange", db.scalar(tq.where(ToolRequest.kind == "return")) or 0, "сдают инструмент — принять",
                 "/stock/issues?tab=tools", info=info_new, tile=['Инструмент'])
            _add(out, "/tools", "orange", db.scalar(select(func.count()).select_from(ToolRequest).where(
                ToolRequest.branch_id == bid, ToolRequest.status == "ready", ToolRequest.kind == "issue")) or 0,
                 "инструмент подготовлен — ещё не выдан", "/stock/issues?tab=tools", info=info_new, tile=['Инструмент'])
        # 19.11.2026 (брат): «готово — ещё не выдано» снова горит оранжевым (18.11 убирали) — пока не нажато «Выдано»

    # ---------- Мойка (18.11.2026, брат) ----------
    wash_queue = ctx.can("orders.create", "orders.approve", "orders.view_all")   # 20.11.2026: запчастисту — только химия
    if ctx.can("wash.view") and not wash_queue and ctx.can("wash.buy"):
        # 🟠 20.11.2026 (брат): химию для мойки заказывает запчастист — заканчивается / черновик закупки химии
        from . import chem as chem_svc
        from ..models import Purchase as _P
        _add(out, "/wash", "orange", sum(1 for x in chem_svc.items(db, bid) if x["free"] <= 2),
             "химия заканчивается — заказать", "/wash?tab=chem", tile=['Заканчивается'])
        _add(out, "/wash", "orange", db.scalar(select(func.count()).select_from(_P).where(
            _P.branch_id == bid, _P.kind == "chem", _P.status == "draft", _P.created_by == me.id)) or 0,
             "черновик закупки химии — нажмите «✓ Заказал»", "/wash?tab=buy", tile=['Закупка для мойки'])
    if ctx.can("wash.view") and wash_queue:
        from ..models import ChemRequest
        wq = select(WorkOrder.id).where(WorkOrder.branch_id == bid, WorkOrder.kind == "wash",
                                        WorkOrder.status.in_(("ready", "paid")))
        if not see_all:
            wq = wq.where(WorkOrder.manager_id == me.id)
        ids_ = db.scalars(wq).all()
        _add(out, "/wash", "green", len(ids_), "помыты — позвонить клиенту, оплата и выдача", "/wash?tab=now",
             wo=ids_, blk="#money", rows=["#btn-pay", "#btn-handover"], tile=['Очередь'])
        if not ctx.can("stock.issue"):   # у кладовщика — у «Запросов на выдачу»
            _add(out, "/wash", "orange", db.scalar(select(func.count()).select_from(ChemRequest).where(
                ChemRequest.branch_id == bid, ChemRequest.status == "new")) or 0, "мойщик просит химию — ждёт кладовщика",
                 "/wash?tab=asks", tile=['Просят'])
        # 🔴 19.11.2026: мойщик просит изменить очередь — ждёт ответа менеджера
        if ctx.can("orders.approve"):
            from ..models import WashQueueRequest
            if db.scalar(select(WashQueueRequest.id).where(WashQueueRequest.branch_id == bid,
                                                           WashQueueRequest.status == "new").limit(1)):
                from . import wash as wash_svc
                _add(out, "/wash", "red", len(wash_svc.open_asks(db, bid)), "мойщик просит изменить очередь",
                     "/wash?tab=asksq", tile=['Изменить очередь'])
    # 20.11.2026 (брат): красной «в очереди — никто не взял» у мойщика больше нет — очередь машин входит в его
    # оранжевую «ждут мойки — ваши»; красная у мойщика — только «вам передали наряд»

    # ---------- Склад, Закупки (кладовщик) ----------
    if ctx.can("stock.receive") and role != "content":   # кладовщик разбора обычные поставки не принимает
        # 18.11.2026: ждём приход — оранжевым (подготовиться принять); опаздывают (прошла дата «привезут») — красным
        wait_q = (select(func.count()).select_from(PurchaseLine).join(Purchase, Purchase.id == PurchaseLine.purchase_id)
                  .where(Purchase.branch_id == bid, Purchase.status.in_(("ordered", "partial")), PurchaseLine.received.is_(False)))
        late = db.scalar(wait_q.where(Purchase.expected_on.is_not(None), Purchase.expected_on < date.today())) or 0
        _add(out, "/stock", "red", late, "опаздывают — позвонить поставщику", "/stock/receive?tab=late", info=info_new,
             sel=[("/stock/receive", ".rc-group:not(.rc-inv-group)")], tile=['Опаздывают'])
        _add(out, "/stock", "orange", (db.scalar(wait_q) or 0) - late, "ждём приход — подготовиться принять", "/stock/receive?tab=wait",
             info=info_new, sel=[("/stock/receive", ".rc-group:not(.rc-inv-group)")], tile=['Ждём'])
    # 17.11.2026 (брат): «Всегда в наличии» — сколько позиций ниже нормы, сиреневая цифра у «Склада»
    if ctx.can("stock.keep"):
        from . import keep as ksvc
        _add(out, "/stock", "purple", ksvc.below_count(db, bid), "«Всегда в наличии» — ниже нормы", "/stock/always", info=info_new,
             sel=[("/stock/always", 'tr[data-below="1"]')], tile=['Не хватает', 'Едет или на грани'])
    # 17.11.2026 (брат): в «Закупках» у кладовщика — только свои заказы на склад; цифра — его черновики («Заказал» не нажат)
    if ctx.can("purchases.stock") and not ctx.can("purchases.manage"):
        _add(out, "/purchases", "orange", db.scalar(select(func.count()).select_from(Purchase).where(
            Purchase.branch_id == bid, Purchase.status == "draft", Purchase.created_by == me.id)) or 0,
             "ваши черновики — нажмите «Заказан»", "/purchases?tab=draft", tile=['Черновики'])

    # ---------- 19.11.2026 (брат): клиент отказался после согласования — «Лишнее после отказов» ----------
    if ctx.can("parts.pick", "stock.manage"):
        from ..models import PartLeftover
        lq = select(func.count()).select_from(PartLeftover).where(PartLeftover.branch_id == bid)
        # 19.11.2026 (брат): цифра — у «Склада», где плитка «Лишнее после отказов» (раньше у запчастиста была у
        # «Моих заказов», и на «Складе» плитка была, а кружка в меню — нет)
        if ctx.can("parts.pick"):   # 🔴 заказано у поставщика — отменить или пусть приедет (решает запчастист)
            _add(out, "/stock", "red", db.scalar(lq.where(PartLeftover.status == "decide")) or 0,
                 "клиент отказался — решите с заказом у поставщика", "/stock/leftovers",
                 sel=[("/stock/leftovers", '.lo-card[data-st="decide"]')], tile=['Заказ у поставщика'])
        _add(out, "/stock", "orange", db.scalar(lq.where(PartLeftover.status == "stock")) or 0,
             "лишнее после отказа на складе — оставить, вернуть или продать", "/stock/leftovers",
             sel=[("/stock/leftovers", '.lo-card[data-st="stock"]')],
             info=info_new and not ctx.can("parts.pick"), tile=['На складе — решить'])

    # ---------- Мои заказы (запчастист) ----------
    if ctx.can("purchases.mine") and not ctx.can("purchases.by_partsman"):
        from . import stock as ssvc
        rows = ssvc.partsman_lines(db, bid, me.id, days=1)
        _add(out, "/my-orders", "red", sum(1 for r in rows if r["late"]), "опаздывают", "/my-orders?st=late", tile=['Опаздывают'])
        # 12.11.2026: черновик под наряд не отправлен поставщику — в наряде это «в черновике», кладовщик ждёт
        _add(out, "/my-orders", "orange", len(ssvc.draft_waits(db, bid, me.id)), "черновики под наряды — нажмите «✓ Заказал»",
             "/my-orders?st=todo", tile=['Заказать'])   # 22.11.2026: вкладка «Заказать» = st=todo (st=draft такой вкладки нет)
        # 18.11.2026 (брат): «приняты на склад сегодня» — только для сведения, цифру убрали

    # ---------- Разбор: выдача и отправка ----------
    if ctx.can("razbor.manage"):
        base = select(func.count()).select_from(UsedSale).where(UsedSale.branch_id == bid, UsedSale.status == "reserved")
        _add(out, "/razbor/shipping", "red", db.scalar(base.where(UsedSale.packed_at.is_(None))) or 0, "подготовить",
             "/razbor/shipping?st=prepare", info=info_rz, tile=['Подготовить'],
             sel=[("/razbor/shipping", '.sh-card[data-stage="prepare"]')])
        # 23.11.2026 (брат): «готово» — самовывоз и отправка отдельно, каждая ведёт на свою плитку (раньше одна цифра
        # вела на «Готово к отправке», а готовый товар был самовывозом — на плитке 0)
        ship = UsedSale.delivery.in_(("np", "other"))
        _add(out, "/razbor/shipping", "orange", db.scalar(base.where(UsedSale.packed_at.is_not(None), or_(UsedSale.delivery.is_(None), ~ship))) or 0,
             "готово — ждёт покупателя (самовывоз)", "/razbor/shipping?st=ready_pickup", info=info_rz,
             tile=['Готово к выдаче'], sel=[("/razbor/shipping", '.sh-card[data-stage="ready_pickup"]')])
        _add(out, "/razbor/shipping", "orange", db.scalar(base.where(UsedSale.packed_at.is_not(None), ship)) or 0,
             "готово — отдать в службу доставки", "/razbor/shipping?st=ready_ship", info=info_rz,
             tile=['Готово к отправке'], sel=[("/razbor/shipping", '.sh-card[data-stage="ready_ship"]')])

    # ---------- Цены разбора (18.11.2026, брат) ----------
    if ctx.can("razbor.price") or ctx.can("razbor.manage"):
        from . import razbor_price as rp
        rc = rp.counts(db, bid)
        if ctx.can("razbor.price"):
            _add(out, "/razbor/prices", "red", rc["new"], "новые детали — утвердить цену", "/razbor/prices?tab=new", tile=['Новые детали'])
            _add(out, "/razbor/prices", "orange", rc["change"], "просят изменить цену", "/razbor/prices?tab=change", tile=['Изменение цены'])
        if ctx.can("razbor.manage"):
            _add(out, "/razbor", "red", rc["declined"], "отказано в утверждении — посмотреть причину", "/razbor?v=parts&f=declined",
                 info=info_rz, tile=['Отказано'])

    # ---------- «На сайт» (22.11.2026, брат): новые запчасти на продажу ----------
    if ctx.can("razbor.price") or ctx.can("razbor.export"):
        from . import site as site_svc
        sc = site_svc.counts(db, bid, sync=False)
        if ctx.can("razbor.price"):
            _add(out, "/site", "red", sc["wait"], "новые на продажу — утвердить цену", "/site?t=new&f=wait", tile=['Ждут утверждения'])
        if ctx.can("razbor.export"):
            _add(out, "/site", "red", sc["declined"], "директор отказал — посмотреть причину", "/site?t=new&f=declined", tile=['Отказано'])
            _add(out, "/site", "orange", sc["nophoto"], "на сайте без фото — добавить фото", "/site?t=new&f=site", tile=['На сайте'])

    # ---------- Зарплата: прошлая неделя ждёт проверки / утверждения ----------
    # 20.11.2026 (брат): цифры — у «Зарплаты сотрудников»: бухгалтеру 🟠 проверить / 🟢 выдать; директору (и заму, пока у
    # него доступ) 🟠 утвердить — проверенные бухгалтером; админу — всё, что не утверждено
    approver = ctx.can("payroll.view_all") and ctx.can("payroll.approve", "payroll.edit")
    if ctx.can("payroll.check") or approver:
        from . import payweek as wsvc
        from .staff import staff_for_period
        start = wsvc.monday(date.today()) - timedelta(days=7)
        users = staff_for_period(db, bid, start)
        data = wsvc.week_data(db, bid, start, users)
        act = [r for r in data["rows"].values() if r["accrued"] > 0 and r["state"] != "paid"]
        link = f"/payroll/weeks?d={start.isoformat()}"
        if ctx.can("payroll.check"):
            _add(out, "/payroll/weeks", "orange", sum(1 for r in act if r["pst"] == "open"), "проверить", link,
                 sel=[("/payroll/weeks", '.pw-user[data-pst="open"]')], tile=["Проверить"])
        if approver:
            states = ("open", "checked") if ctx.can("payroll.edit") and role == "admin" else ("checked",)
            _add(out, "/payroll/weeks", "orange", sum(1 for r in act if r["pst"] in states), "утвердить к выплате", link,
                 sel=[("/payroll/weeks", f'.pw-user[data-pst="{s}"]') for s in states], tile=['Утвердить'])
        if ctx.can("payroll.pay"):
            _add(out, "/payroll/weeks", "green", sum(1 for r in act if r["pst"] == "approved"), "выдать", link,
                 sel=[("/payroll/weeks", '.pw-user[data-pst="approved"]')], tile=["Выдать"])
    # 🔴 20.11.2026: зам просит доступ к «Зарплате сотрудников» — директору
    if ctx.can("payroll.grant"):
        from ..models.payroll import PayrollAccess
        _add(out, "/payroll/weeks", "red", db.scalar(select(func.count()).select_from(PayrollAccess).where(
            PayrollAccess.branch_id == bid, PayrollAccess.status == "pending")) or 0,
             "зам просит доступ к «Зарплате сотрудников»", "/access-requests#payroll", tile=['Зарплата'])
    # 20.11.2026 (брат): «Напомнить о записи» — 🔴 сегодня не приехали (время прошло), 🟠 завтра — позвонить сегодня
    if ctx.can("schedule.manage"):
        from . import schedule as sch
        rm = sch.reminders(db, bid)
        _add(out, "/schedule", "red", len(rm["late"]), "сегодня не приехали — позвонить", "/schedule/remind#late", tile=['Не приехали'])
        _add(out, "/schedule", "orange", len(rm["call"]), "завтра записаны — напомнить", "/schedule/remind#call", tile=['Завтра'])
    # 21.11.2026 (брат): мастеру — его записи: 🔴 не приехали и 🟣 новые (до «✓ Видел»), 🟢 приехали (до «Начал»),
    # 🟠 сегодня ждём
    elif me.role and me.role.code in MASTERS and ctx.can("schedule.view"):
        from . import schedule as sch
        sg = sch.master_signals(db, bid, me.id)
        _add(out, "/schedule", "red", len(sg["noshow"]), "клиент не приехал — отметьте «Видел»", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="noshow"]')], tile=['Не приехали'])
        _add(out, "/schedule", "orange", len(sg["wait"]), "сегодня ждём клиентов", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="wait"]')], tile=['Ждём'])
        _add(out, "/schedule", "purple", len(sg["new"]), "к вам записали — отметьте «Видел»", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="new"]')], tile=['Новые записи'])
        _add(out, "/schedule", "green", len(sg["arrived"]), "клиент приехал — начните работу", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="arrived"]')], tile=['Приехали'])
    return _compact(out)
