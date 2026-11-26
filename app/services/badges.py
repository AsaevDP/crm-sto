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
         wo=None, blk: str = "", rows=None, sel=None, tile=None, btn: str = "", big: bool = False, shape: str = "",
         owner: bool = False):
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
    # 23.11.2026 (брат): нажали на кружок — сначала окошко со списком дел (go), переход — только выбрав дело
    item = {"t": title + (" (для сведения)" if info else ""), "href": link or href, "n": int(n)}
    if same:   # одного цвета — одной цифрой, подсказка перечисляет всё
        same["n"] += int(n)
        same["t"] += f" · {title}"
        same["go"].append(item)
        _merge_marks(same, {"wo": wo, "blk": blk, "rows": rows or [], "sel": sel or [], "tile": list(tile or [])})
    else:
        lst.append({"c": color, "n": int(n), "t": item["t"], "href": link or href,
                    "alone": alone, "info": info, "wo": wo, "blk": blk, "rows": list(rows or []),
                    "sel": [list(s) for s in (sel or [])], "tile": list(tile or []), "btn": btn, "go": [item],
                    "big": big, "shape": shape, "owner": owner})
    # 24.11.2026: big — крупнее (опоздание), shape="warn" — ⚠ вместо цифры. 25.11.2026 (брат): owner — дело, которое
    # может решить ТОЛЬКО админ (владелец): красный кружок с ярко-оранжевым «!» вместо цифры, всегда отдельно (alone)
    lst.sort(key=lambda x: ("red", "orange", "purple", "green").index(x["c"]))   # purple — «Всегда в наличии» (17.11.2026)


def _merge_marks(a: dict, b: dict) -> None:
    """Слили два кружка в один — и места для рамки тоже."""
    a["wo"] = list(dict.fromkeys(a.get("wo", []) + b.get("wo", [])))
    a["blk"] = a.get("blk") or b.get("blk", "")
    a["rows"] = a.get("rows", []) + b.get("rows", [])
    a["sel"] = a.get("sel", []) + [list(s) for s in b.get("sel", [])]
    a["tile"] = list(dict.fromkeys(a.get("tile", []) + b.get("tile", [])))
    a["btn"] = a.get("btn") or b.get("btn", "")
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
                    # 23.11.2026 (брат): склеенный кружок ведёт в несколько мест — при нажатии меню «куда перейти»
                    same["go"] = list(same.get("go") or []) + list(x.get("go") or [{"t": x["t"], "href": x["href"], "n": x["n"]}])
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
            _add(out, "/orders", "red", len(ids_lead) + len(ids_extra), "диагностика — записать проблемы", "/orders?tab=intake",
                 wo=[*ids_lead, *ids_extra], blk="#problems", rows=["#btn-diag-done", "#btn-diag-mgr", "#btn-add-finding"],
                 tile=['Приём и диагностика'], btn="blue")
        if role in MASTERS:
            # 🔴 23.11.2026 (брат, наряд 25): выдали под машину запчасти / инструмент / химию — мастер подтверждает
            # «✓ Получил». Цифра у «Нарядов», ведёт в наряд к кнопке; пока не подтвердил запчасти, «Начал» серая.
            # Мойщику / детейлеру «Наряды» — очередь мойки: ведёт туда, машина обведена, кнопка — в его наряде мойки
            from . import receipt as _rc
            from . import chem as _chem
            washer_ = role in ("washer", "detailer")
            rc_wo = [] if washer_ else [wo.id for _, wo in _rc.for_master(db, bid, me.id)]
            rc_wo += [r.work_order_id for r in _rc.tools_for_master(db, bid, me.id) if r.work_order_id]
            rc_wo += [r.work_order_id for r in _chem.unreceived_for(db, bid, me.id) if r.work_order_id]
            rc_wo = list(dict.fromkeys(rc_wo))
            if washer_:
                _add(out, "/orders", "red", len(rc_wo), "Подтвердить получение — выдали химию или инструмент для машины",
                     "/wash/queue", sel=[("/wash/queue", ".wq-card.rcv")])
            else:
                _add(out, "/orders", "red", len(rc_wo), "Подтвердить получение — выдали запчасти или инструмент",
                     "/orders?tab=active", wo=rc_wo, blk="#parts", rows=[".btn-recv"], tile=['На СТО'], btn="blue")
        if ctx.can("orders.approve"):
            # 🔴 23.11.2026 (брат): шаг «Проблемы» — дело менеджера: после приёмки (записать со слов клиента или отправить
            # на диагностику) и когда мастер закончил диагностику, а проблемы «впишет менеджер»
            # 25.11.2026 (брат): клиент приехал по записи — ведущему менеджеру 🔴 «оформите приёмку» (кружок = сколько машин)
            from .arrival import pending_for
            ids_arr = [o.id for o in pending_for(db, bid, me.id)]
            _add(out, "/orders", "red", len(ids_arr), "клиент приехал по записи — оформите приёмку",
                 "/orders?tab=intake", wo=ids_arr, blk="#intake-pending", rows=["#btn-intake"],
                 tile=['Приём и диагностика'], btn="blue")
            ids_new = [i for i in ids_status("created", field=None if see_all else WorkOrder.manager_id)
                       if (db.get(WorkOrder, i).kind or "") != "wash" and not db.get(WorkOrder, i).intake_pending]
            _add(out, "/orders", "red", len(ids_new), "записать проблемы или отправить на диагностику", "/orders?tab=intake",
                 wo=ids_new, blk="#problems", rows=["#btn-add-finding", "#btn-to-approve"], tile=['Приём и диагностика'], btn="blue")
            from . import handover as _hs
            ids_mgr = [o.id for o in db.scalars(select(WorkOrder).where(
                WorkOrder.id.in_(ids_status("diag", field=None if see_all else WorkOrder.manager_id)),
                WorkOrder.problems_by_manager.is_(True))) if all(s["done"] for s in _hs.diag_state(db, o))]
            _add(out, "/orders", "red", len(ids_mgr), "мастер закончил диагностику — впишите проблемы", "/orders?tab=intake",
                 wo=ids_mgr, blk="#problems", rows=["#btn-add-finding", "#btn-to-approve"], tile=['Приём и диагностика'], btn="blue")
        # 🔴 13.11.2026: мне передали наряд (менеджеру, мастеру, запчастисту) — горит, пока не нажал «✓ Принял»
        from .handover import pending_orders
        # 19.11.2026: у мойщика «Наряды» = очередь мойки — там блок «Вам передали»
        handed = pending_orders(db, bid, me.id)
        washer_ = role in ("washer", "detailer")
        _add(out, "/orders", "red", len(handed), "вам передали наряд — нажмите «Принял»",
             "/wash/queue#handed" if washer_ else "/orders?tab=handed",
             wo=[] if washer_ else handed, blk="#ho-accept", tile=[] if washer_ else ['Мне передали'])   # у мойщика плиток нет — блок «Вам передали»
        # 23.11.2026 (брат): согласование по шагам — Работы (менеджер) → Подбор (запчастист) → Звонок (менеджер).
        # Кружки загораются по очереди; одновременно — только «впишите работы» (🔴 менеджеру) и «скоро подбор»
        # (🟠 запчастисту).
        from .orders import approve_phase
        if ctx.can("orders.approve"):
            ap = [db.get(WorkOrder, i) for i in ids_status("approve", field=None if see_all else WorkOrder.manager_id)]
            ids_w = [o.id for o in ap if approve_phase(o) == "works"]
            ids_c = [o.id for o in ap if approve_phase(o) == "call"]
            _add(out, "/orders", "red", len(ids_w), "впишите работы и цены — отправьте на подбор", "/orders?tab=approve",
                 wo=ids_w, blk="#works", rows=["#btn-works-sent"], tile=['Согласование'], btn="blue")
            # 22.11.2026 (брат): красная — дело менеджера, пока не согласует, работа стоит
            _add(out, "/orders", "red", len(ids_c), "подбор готов — звоните клиенту, согласуйте", "/orders?tab=approve",
                 wo=ids_c, blk="#works", rows=['.work[data-appr="pending"]', '.part[data-appr="pending"]',
                                               '.optgroup[data-appr="pending"]', "#btn-approve-done"], tile=['Согласование'],
                 btn="blue")
        if ctx.can("parts.pick") and not ctx.can("orders.approve"):
            # 13.11.2026: подбор идёт уже на согласовании — пока не нажал «Подбор завершён»; 23.11.2026 — после того,
            # как менеджер записал работы
            qp = select(WorkOrder).where(WorkOrder.branch_id == bid, WorkOrder.status.in_(("picking", "approve")))
            if not see_all:
                qp = qp.where(WorkOrder.partsman_id == me.id)
            mine = db.scalars(qp).all()
            ids_ = [o.id for o in mine if o.status == "picking" or approve_phase(o) == "picking"]
            soon = [o.id for o in mine if o.status == "approve" and approve_phase(o) == "works"]
            # 23.11.2026 (брат): красная — пока не подобрал, менеджер не может звонить клиенту (работа стоит)
            _add(out, "/orders", "red", len(ids_), "подобрать запчасти", "/orders?tab=approve", wo=ids_, blk="#parts",
                 rows=["#btn-picked"], tile=['Согласование'], btn="blue")
            # 🟠 проблемы записаны, менеджер пишет работы — скоро подбор (подготовиться)
            _add(out, "/orders", "orange", len(soon), "скоро подбор — менеджер пишет работы", "/orders?tab=approve",
                 wo=soon, blk="#parts", rows=["#parts-wait-works"], tile=['Согласование'])
            # 25.11.2026 (брат, наряд 39): менеджер согласовал — запчастисту 🟠 и у «Нарядов»: заказать у поставщика
            # (тот же список, что «Мои заказы → Заказать»; гаснет, когда нажал «✓ Заказал»)
            if ctx.can("purchases.mine"):
                from . import stock as _ssvc
                ids_o = [d["wo"].id for d in _ssvc.draft_waits(db, bid, me.id)]
                _add(out, "/orders", "orange", len(ids_o), "согласовано — закажите у поставщика", "/orders?tab=parts",
                     wo=ids_o, blk="#parts", rows=['.part[data-draft="1"]', "#btn-order-wo"], tile=['Запчасти'])
        if ctx.can("orders.approve"):
            ids_ = ids_status("ready", field=None if see_all else WorkOrder.manager_id)
            _add(out, "/orders", "green", len(ids_), "готовы к выдаче клиенту", "/orders?tab=ready", wo=ids_, blk="#money",
                 rows=["#btn-pay", "#btn-handover"], tile=['Готовы к выдаче'], btn="blue")
        # 🟢 12.11.2026: пришли запчасти по закупке и ждут выдачи — менеджеру, запчастисту и всем мастерам наряда.
        # Цифра = сколько запчастей. Гаснет, когда кладовщик выдал мастеру. Отдельной цифрой — не смешивается с нарядами.
        arrived = {}
        # 20.11.2026 (брат): мойщику и детейлеру запчасти не нужны — этих цифр у них нет
        if ctx.can("orders.approve", "parts.pick") or (role in MASTERS and role not in ("washer", "detailer")):
            from .notify import arrived_parts, arrived_waiting
            arrived = arrived_waiting(db, bid, me.id)   # всё пришедшее и не выданное — ниже, для «можно начинать»
            # 23.11.2026 (брат): 🔴 пришли запчасти, а выдачу никто не запросил — тем, кто может нажать «Запросить
            # выдачу» (менеджер, мастера наряда); запросили — гаснет. 🟢 менеджеру: запчасти ВЫДАНЫ, ждём «✓ Получил»
            # от мастера; подтвердил — гаснет. Подготовлено, но не выдано — здесь ничего (🔴 «Забрать у окошка» в
            # «Запросах»). 24.11.2026 (брат, наряды 23, 27): цифра = НАРЯДЫ (как плитка «Пришли запчасти»), мигают все
            # детали наряда, которые ждут, — и с полки склада (notify.arrived_parts).
            if ctx.can("stock.request_issue"):
                ask = arrived_parts(db, bid, me.id, "ask")
                ask_rows = arrived_parts(db, bid, me.id, "ask", part_ids=True) if ask else []
                _add(out, "/orders", "red", len(ask), "пришли запчасти — запросите выдачу", "/orders?tab=arrived",
                     alone=True, wo=list(ask), blk="#parts", rows=[f'.part[data-id="{i}"]' for i in ask_rows] + ["#btn-request-issue"],
                     tile=['Пришли запчасти'], btn="blue")
            # 23.11.2026 (брат): запчастисту — 🟣 для сведения: по его нарядам пришло и ещё не выдано (серая плитка)
            if not ctx.can("stock.request_issue", "orders.approve") and ctx.can("parts.pick"):
                from .notify import partsman_info
                pinfo = partsman_info(db, bid, me.id)
                p_rows = partsman_info(db, bid, me.id, part_ids=True) if pinfo else []
                _add(out, "/orders", "purple", len(pinfo), "пришли запчасти — ждут выдачи", "/orders?tab=arrived",
                     alone=True, info=True, wo=list(pinfo), blk="#parts", tile=['Пришли запчасти'],
                     rows=[f'.part[data-id="{i}"]' for i in p_rows])   # в наряде мигают все такие детали; блок не обводим
            if ctx.can("orders.approve"):
                going = arrived_parts(db, bid, me.id, "going")
                going_rows = arrived_parts(db, bid, me.id, "going", part_ids=True) if going else []
                _add(out, "/orders", "green", len(going), "запчасти выданы — ждём «✓ Получил» мастера",
                     "/orders?tab=arrived", alone=True, wo=list(going), blk="#parts",
                     rows=[f'.part[data-id="{i}"]' for i in going_rows], tile=['Пришли запчасти'])
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
                 wo=ids_, blk="#parts", rows=["#btn-request-issue"], tile=['Запчасти'], btn="blue")

    # 🔴 менеджер просит доступ ко всем нарядам — админу / директору / заму
    if ctx.can("orders.grant_access"):
        from ..models import OrderAccess
        # 23.11.2026 (брат): просьбы о доступе (ко всем нарядам и к клиенту) — кружок только у «Клиентов», без дубля у «Нарядов»
        from ..models import ClientAccess   # 12.11.2026: менеджер просит доступ к клиенту
        # 20.11.2026 (брат): у «Клиентов» цифра = плитке «Просят доступ»: к одному клиенту + ко всем нарядам (вся база)
        _add(out, "/clients", "red", db.scalar(select(func.count()).select_from(ClientAccess).where(
            ClientAccess.branch_id == bid, ClientAccess.status == "pending")) or 0, "менеджер просит доступ к клиенту",
             "/access-requests#clients", sel=[("/access-requests", ".ac-card.now")], tile=['К клиенту', 'Просят доступ'])
        _add(out, "/clients", "red", db.scalar(select(func.count()).select_from(OrderAccess).where(
            OrderAccess.branch_id == bid, OrderAccess.status == "pending")) or 0,
             "просят доступ ко всем нарядам и клиентам", "/access-requests#all", sel=[("/access-requests", ".ac-card.now")], tile=['Ко всем нарядам', 'Просят доступ'])
        from ..models import OrderReopen   # 11.11.2026: просят открыть закрытый наряд заново
        _add(out, "/orders", "red", db.scalar(select(func.count()).select_from(OrderReopen).where(
            OrderReopen.branch_id == bid, OrderReopen.status == "pending")) or 0, "просят открыть наряд заново",
             "/access-requests#reopen", sel=[("/access-requests", ".ac-card.now")], tile=['Открыть заново'])

    # ---------- Готово к выдаче (менеджер) ----------
    if ctx.can("stock.pickup") and not ctx.can("stock.issue"):
        from ..models import IssueRequestLine
        n, take_wo, take_ids = 0, [], []
        for r, wo in db.execute(select(IssueRequest, WorkOrder).join(WorkOrder, WorkOrder.id == IssueRequest.work_order_id)
                                .where(IssueRequest.branch_id == bid, IssueRequest.status == "ready")):
            if wo.manager_id == me.id or r.created_by == me.id:
                n += 1
                take_wo.append(wo.id)
                take_ids += db.scalars(select(IssueRequestLine.wo_part_id).where(IssueRequestLine.request_id == r.id)).all()
        # 🔴 24.11.2026 (брат, наряд 27; было 🟠, ещё раньше 🟢): кладовщик подготовил — менеджеру забрать у окошка и
        # отнести мастеру: это его дело, красным. В наряде мигают ВСЕ детали подготовленного запроса (раньше мигали
        # только приехавшие по закупке — кружок «Пришли запчасти», взятые с полки не мигали)
        _add(out, "/ready", "red", n, "кладовщик подготовил — забрать у окошка", "/ready#mine", tile=['Забрать у окошка'],
             wo=take_wo, rows=[f'.part[data-id="{i}"]' for i in dict.fromkeys(take_ids) if i])
        # 🔴 20.11.2026 (брат, наряд 60): мастер просит со склада под мой наряд — согласовать / отказать
        from . import messages as msg_svc
        _add(out, "/ready", "red", len(msg_svc.my_pending(ctx)), "мастер просит со склада — согласуйте", "/ready#asks", tile=['Согласовать'])
        # 19.11.2026 (брат, наряд 60): по моим нарядам мастер попросил у кладовщика — менеджер видит и запчасти, и
        # инструмент: 🟠 ждёт кладовщика (проследить), 🟢 инструмент подготовлен — мастер заберёт
        from ..models import ToolRequest
        # 23.11.2026 (брат): в наряде у менеджера мигают сиреневые полоски деталей, которые попросили выдать
        wait_wo = db.scalars(select(IssueRequest.work_order_id).join(
            WorkOrder, WorkOrder.id == IssueRequest.work_order_id).where(
            IssueRequest.branch_id == bid, IssueRequest.status == "new", WorkOrder.manager_id == me.id)).all()
        # 🟣 24.11.2026 (брат, наряд 27; было 🟠): ждут кладовщика — для сведения, от менеджера ничего не зависит
        _add(out, "/ready", "purple", len(wait_wo), "запчасти — ждут кладовщика", "/ready#waiting", tile=['Ждут кладовщика'],
             info=True, wo=wait_wo, rows=['.part[data-asked="1"]'])
        tq = select(func.count()).select_from(ToolRequest).join(WorkOrder, WorkOrder.id == ToolRequest.work_order_id).where(
            ToolRequest.branch_id == bid, ToolRequest.kind == "issue", WorkOrder.manager_id == me.id)
        # 19.11.2026 (брат): инструмент — как запчасти, только у «Запросов на выдачу» (там же «Выдано»);
        # у «Инструмента» цифр нет (сиреневую «для сведения» убрали)
        _add(out, "/ready", "purple", db.scalar(tq.where(ToolRequest.status == "new")) or 0,
             "инструмент — ждёт кладовщика", "/ready#waiting", info=True, tile=['Ждут кладовщика', 'Инструмент'])
        _add(out, "/ready", "red", db.scalar(tq.where(ToolRequest.status == "ready")) or 0,
             "инструмент подготовлен — забрать у окошка и отнести мастеру", "/ready#tools", tile=['Забрать у окошка'])

    # ---------- Мои запросы на выдачу (мастер, 12.11.2026) ----------
    if role in MASTERS:
        from ..models import ChemRequest, OrderMessage, ToolRequest, WoWork
        my_wo = select(WorkOrder.id).where(WorkOrder.branch_id == bid, or_(
            WorkOrder.mechanic_id == me.id, WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me.id))))
        mine_req = or_(IssueRequest.created_by == me.id, IssueRequest.work_order_id.in_(my_wo))
        if role in ("washer", "detailer"):   # 20.11.2026: запчасти слесарей в машине, которую он моет, — не его
            mine_req = IssueRequest.created_by == me.id
        # 23.11.2026 (брат): «готово — идите забирать» — 🔴 (важно, было 🟢); «выдали — подтвердите» — тоже 🔴
        _add(out, "/my-requests", "red", db.scalar(select(func.count()).select_from(IssueRequest).where(
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
        # 🔴 23.11.2026 (было 🟢): кладовщик подготовил инструмент / химию — «готово, подойдите», как с запчастями
        _add(out, "/my-requests", "red", db.scalar(tq.where(ToolRequest.status == "ready")) or 0,
             "инструмент готов — забрать у кладовщика", "/my-requests?tab=tools", tile=['Инструмент'])
        _add(out, "/my-requests", "red", db.scalar(cq.where(ChemRequest.status == "ready")) or 0,
             "химия готова — забрать у кладовщика", "/my-requests?tab=chem", tile=['Химия'])
        _add(out, "/my-requests", "orange", db.scalar(tq.where(ToolRequest.status == "new")) or 0,
             "инструмент — ждёт кладовщика", "/my-requests?tab=tools", tile=['Ждут кладовщика', 'Инструмент'])
        _add(out, "/my-requests", "orange", db.scalar(cq.where(ChemRequest.status == "new")) or 0,
             "химия — ждёт кладовщика", "/my-requests?tab=chem", tile=['Химия'])
        # 🔴 23.11.2026 (брат): выдали БЕЗ машины (в запас: полировальная машинка, банка химии на несколько машин) —
        # «Подтвердить получение» здесь. Выданное под машину — у «Нарядов» (одно дело — один кружок, дублей нет)
        from . import receipt as rsvc
        from . import chem as _chem
        t_free = [r for r in rsvc.tools_for_master(db, bid, me.id) if not r.work_order_id]
        c_free = [r for r in _chem.unreceived_for(db, bid, me.id) if not r.work_order_id]
        _add(out, "/my-requests", "red", len(t_free), "Подтвердить получение — выдали инструмент", "/my-requests?tab=tools",
             tile=['Инструмент'], sel=[("/my-requests", "#col-tools .btn-recv")])
        _add(out, "/my-requests", "red", len(c_free), "Подтвердить получение — выдали химию", "/my-requests?tab=chem",
             tile=['Химия'], sel=[("/my-requests", "#col-chem .btn-recv")])

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
                 "/stock/issues?tab=tools", info=info_new, tile=['Инструмент', 'Просят / сдают'])
            _add(out, "/tools", "orange", db.scalar(tq.where(ToolRequest.kind == "return")) or 0, "сдают инструмент — принять",
                 "/stock/issues?tab=tools", info=info_new, tile=['Инструмент', 'Просят / сдают'])
            _add(out, "/tools", "orange", db.scalar(select(func.count()).select_from(ToolRequest).where(
                ToolRequest.branch_id == bid, ToolRequest.status == "ready", ToolRequest.kind == "issue")) or 0,
                 "инструмент подготовлен — ещё не выдан", "/stock/issues?tab=tools", info=info_new, tile=['Инструмент', 'Просят / сдают'])
        # 19.11.2026 (брат): «готово — ещё не выдано» снова горит оранжевым (18.11 убирали) — пока не нажато «Выдано»

    # ---------- Только админ (25.11.2026, брат): проверка инструмента ждёт его решения по удержаниям ----------
    if role == "admin":
        from ..models import ToolCheck
        n_chk = db.scalar(select(func.count()).select_from(ToolCheck).where(ToolCheck.branch_id == bid,
                                                                             ToolCheck.status == "done")) or 0
        _add(out, "/tools", "red", n_chk, "проверка инструмента — решить, что удержать из зарплаты",
             "/tools?view=staff#checks", alone=True, owner=True, sel=[("/tools", "#checks .kv")], tile=['Проверки'])

    # ---------- Инструмент после закрытия наряда (24.11.2026, брат) ----------
    # Общий инструмент под наряд, наряд закрыт / отменён, а инструмент у мастера. Мастеру: 🔴 сдать (красная «Сдаю»),
    # нажал «Сдаю» — 🟠 до «Принял» / «Вернул» кладовщика. Руководству (кладовщик, главный, менеджер — свои наряды,
    # директор, зам) — 🟠 «не сдали»; кладовщику без уже нажатых «Сдаю» (те — в «сдают инструмент — принять»).
    if ctx.can("tools.view_own", "tools.view"):
        from . import tools as tools_svc
        mine_late = tools_svc.after_close(db, bid, holder_id=me.id)
        my_page = f"/tools/staff/{me.id}"
        _add(out, "/tools", "red", sum(1 for x in mine_late if not x["ret"]),
             "наряд закрыт — сдайте инструмент в чистом и исправном виде", my_page + "#late", alone=True,
             sel=[(my_page, ".btn-ret-late")])
        _add(out, "/tools", "orange", sum(1 for x in mine_late if x["ret"]),
             "инструмент сдаёте — ждёт кладовщика", my_page + "#late", alone=True, sel=[(my_page, ".ret-wait-late")])
        if ctx.can("tools.view"):
            keeper = ctx.can("tools.manage")
            late_all = tools_svc.after_close(db, bid, manager_id=None if (keeper or ctx.can("orders.grant_access", "staff.dismiss")
                                                                            or role in ALL_ROLES) else me.id)
            n = sum(1 for x in late_all if not (keeper and x["ret"]) and x["tool"].holder_id != me.id)
            _add(out, "/tools", "orange", n, "наряд закрыт, инструмент не сдан", "/tools?view=late",
                 sel=[("/tools?view=late", ".tl-late-row:not(.tl-late-ret)" if keeper else ".tl-late-row")], tile=['Не сдали после наряда'])

    # ---------- Мойка (18.11.2026, брат) ----------
    wash_queue = ctx.can("orders.create", "orders.approve", "orders.view_all")   # 20.11.2026: запчастисту — только химия
    if ctx.can("wash.view") and not wash_queue and ctx.can("wash.buy"):
        # 🟠 20.11.2026 (брат): химию для мойки заказывает запчастист — заканчивается / черновик закупки химии
        from . import chem as chem_svc
        from ..models import Purchase as _P
        from . import keep as _ksvc   # 25.11.2026: по норме «Всегда в наличии» (уже заказанное — не горит)
        _add(out, "/wash", "orange", sum(1 for x in _ksvc.chem_with_keep(db, bid, chem_svc.items(db, bid)) if x["low"]),
             "химия заканчивается — заказать", "/wash?tab=chem", sel=[("/wash?tab=chem", "tr.low")], tile=['Заканчивается'])
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
             wo=ids_, blk="#money", rows=["#btn-pay", "#btn-handover"], tile=['Очередь'], btn="blue")
        if not ctx.can("stock.issue"):   # у кладовщика — у «Запросов на выдачу»
            # 22.11.2026 (брат): менеджеру — для сведения (химию выдаёт кладовщик, менеджер не обязан нести) → сиреневая
            _add(out, "/wash", "purple", db.scalar(select(func.count()).select_from(ChemRequest).where(
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
        # 22.11.2026 (брат): с временем привоза — опаздывает, когда прошло обещанное время (без времени — день)
        from . import stock as _st
        late_ids = [p.id for p in db.scalars(select(Purchase).where(
            Purchase.branch_id == bid, Purchase.status.in_(("ordered", "partial")), Purchase.expected_on.is_not(None),
            Purchase.expected_on <= date.today())) if _st.is_late(p)]
        late = db.scalar(wait_q.where(Purchase.id.in_(late_ids))) or 0 if late_ids else 0
        _add(out, "/stock", "red", late, "опаздывают — позвонить поставщику", "/stock/receive?tab=late", info=info_new,
             sel=[("/stock/receive", ".rc-group:not(.rc-inv-group)")], tile=['Опаздывают', 'Приход'])
        _add(out, "/stock", "orange", (db.scalar(wait_q) or 0) - late, "ждём приход — подготовиться принять", "/stock/receive?tab=wait",
             info=info_new, sel=[("/stock/receive", ".rc-group:not(.rc-inv-group)")], tile=['Ждём', 'Приход'])
    # 23.11.2026 (брат): кладовщику по нарядам (своя зона склада) — 🟠 «согласовано — подготовить к выдаче» (гаснет, когда
    # мастер запросил выдачу или выдали) и 🟣 для сведения «отложено со склада под наряд» (гаснет после «Получил» мастера)
    if ctx.can("stock.issue", "razbor.issue"):
        from . import keeper_watch as kw
        zones = kw.zones_for(db, me)
        prep, held = kw.prepare(db, bid, zones), kw.held(db, bid, zones)
        # цифра — сколько нарядов (не деталей), как у остальных кружков по нарядам
        _add(out, "/stock", "orange", len(prep), "согласовано — подготовить к выдаче",
             "/stock/reserved#prep", alone=True, sel=[("/stock/reserved", '.kv[data-prep="1"]')],
             tile=['Подготовить к выдаче', 'В резерве'])
        _add(out, "/stock", "purple", len(held), "отложено со склада под наряд — ждём «Получил» мастера",
             "/stock/reserved#held", alone=True, info=True, sel=[("/stock/reserved", '.kv[data-held="1"]')],
             tile=['Отложено под наряды'])
    # 17.11.2026 (брат): «Всегда в наличии» — сколько позиций ниже нормы, сиреневая цифра у «Склада»
    if ctx.can("stock.keep"):
        from . import keep as ksvc
        # 25.11.2026 (брат): склад (ТО, запчасти) — кладовщику; химия — запчастисту (у него цифра в «Мойке»)
        _add(out, "/stock", "purple", sum(1 for r in ksvc.rows(db, bid) if r["below"] and r["owner"] == "stock"),
             "«Всегда в наличии» — ниже нормы", "/stock/always", info=info_new,
             sel=[("/stock/always", 'tr[data-below="1"]')], tile=['Не хватает', 'Заказано', 'Всегда в наличии'])
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
                 sel=[("/stock/leftovers", '.lo-card[data-st="decide"]')], tile=['Заказ у поставщика', 'Лишнее после отказов'])
        _add(out, "/stock", "orange", db.scalar(lq.where(PartLeftover.status == "stock")) or 0,
             "лишнее после отказа на складе — оставить, вернуть или продать", "/stock/leftovers",
             sel=[("/stock/leftovers", '.lo-card[data-st="stock"]')],
             info=info_new and not ctx.can("parts.pick"), tile=['На складе — решить', 'Лишнее после отказов'])

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
            _add(out, "/razbor/prices", "red", rc["new"], "новые детали — утвердить цену", "/razbor/prices?tab=new", sel=[("/razbor/prices", '.rp-row[data-kind="new"], .rp-car.wait')], tile=['Новые детали'])
            _add(out, "/razbor/prices", "orange", rc["change"], "просят изменить цену", "/razbor/prices?tab=change", sel=[("/razbor/prices", '.rp-row[data-kind="change"]')], tile=['Изменение цены'])
        if ctx.can("razbor.manage"):
            _add(out, "/razbor", "red", rc["declined"], "отказано в утверждении — посмотреть причину", "/razbor?v=parts&f=declined",
                 info=info_rz, tile=['Отказано'])
    # 🟠 22.11.2026 (брат): в наличии, но не выложено на сайт — «＋ На сайт»
    if ctx.can("razbor.manage"):
        from . import razbor as rz_svc
        _add(out, "/razbor", "orange", rz_svc.nosite_count(db, bid), "нет на сайте — выложить («＋ На сайт»)",
             "/razbor?v=parts&f=nosite", info=info_rz, tile=['Нет на сайте'], sel=[("/razbor", ".rzp-site")])

    # ---------- «На сайт» (22.11.2026, брат): новые запчасти на продажу ----------
    if ctx.can("razbor.price") or ctx.can("razbor.export"):
        from . import site as site_svc
        sc = site_svc.counts(db, bid, sync=False)
        if ctx.can("razbor.price"):
            _add(out, "/site", "red", sc["wait"], "новые на продажу — утвердить цену", "/site?t=new&f=wait", sel=[("/site", '.sn-row[data-stage="wait"]')], tile=['Ждут утверждения'])
        if ctx.can("razbor.export"):
            _add(out, "/site", "red", sc["declined"], "директор отказал — посмотреть причину", "/site?t=new&f=declined", sel=[("/site", '.sn-row[data-stage="declined"]')], tile=['Отказано'])
            _add(out, "/site", "orange", sc["nophoto"], "на сайте без фото — добавить фото", "/site?t=new&f=site", sel=[("/site", '.sn-row[data-nophoto="1"]')], tile=['На сайте'])

    # 🔴 22.11.2026 (брат): «Заявки с сайта» — новые, принять или отказать
    if ctx.can("site.orders"):
        from . import site_orders as so_svc
        _add(out, "/site/orders", "red", so_svc.count_new(db, bid), "новые заявки с сайта — принять или отказать",
             "/site/orders?tab=new", tile=['Новые'], sel=[("/site/orders", '.so-card[data-st="new"]')])

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
            # 26.11.2026 (брат): плитки — фильтр, кружок ведёт сразу в свою плитку (?f=)
            _add(out, "/payroll/weeks", "orange", sum(1 for r in act if r["pst"] == "open"), "проверить", link + "&f=open",
                 sel=[("/payroll/weeks", '.pw-user[data-pst="open"]')], tile=["Проверить"])
        if approver:
            states = ("open", "checked") if ctx.can("payroll.edit") and role == "admin" else ("checked",)
            _add(out, "/payroll/weeks", "orange", sum(1 for r in act if r["pst"] in states), "утвердить к выплате",
                 link + ("&f=checked" if states == ("checked",) else ""),
                 sel=[("/payroll/weeks", f'.pw-user[data-pst="{s}"]') for s in states], tile=['Утвердить'])
        if ctx.can("payroll.pay"):
            _add(out, "/payroll/weeks", "green", sum(1 for r in act if r["pst"] == "approved"), "выдать", link + "&f=approved",
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
        _add(out, "/schedule", "red", len(rm["late"]), "сегодня не приехали — позвонить", "/schedule/remind#late", sel=[("/schedule", "#btn-remind")], btn="red", tile=['Не приехали'])
        _add(out, "/schedule", "orange", len(rm["call"]), "завтра записаны — напомнить", "/schedule/remind#call", sel=[("/schedule", "#btn-remind")], tile=['Завтра'])
    # 21.11.2026 (брат): мастеру — его записи: 🔴 не приехали и 🟣 новые (до «✓ Видел»), 🟢 приехали (до «Начал»),
    # 🟠 сегодня ждём
    elif me.role and me.role.code in MASTERS and ctx.can("schedule.view"):
        from . import schedule as sch
        sg = sch.master_signals(db, bid, me.id)
        _add(out, "/schedule", "red", len(sg["noshow"]), "клиент не приехал — отметьте «Видел»", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="noshow"]')], tile=['Не приехали'])
        _add(out, "/schedule", "orange", len(sg["wait"]), "сегодня ждём клиентов", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k~="wait"]')], tile=['Ждём'])
        _add(out, "/schedule", "purple", len(sg["new"]), "к вам записали — отметьте «Видел»", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k~="new"]')], tile=['Новые записи'])
        _add(out, "/schedule", "green", len(sg["arrived"]), "клиент приехал — начните работу", "/schedule#zp-sig",
             sel=[("/schedule", '.zp-sig-row[data-k="arrived"]')], tile=['Приехали'])
    # ---------- Табель: опоздания и пропущенные отметки (24.11.2026, брат) ----------
    if ctx.can("attendance.view_own", "attendance.view_all"):
        from . import lateness as late_svc
        mine = late_svc.for_user(db, bid, me)
        # 🔴 крупнее остальных: опоздали — прочитать и «Принял к сведению»
        _add(out, "/timesheet", "red", len(mine["new"]), "вы опоздали — прочитайте и примите к сведению",
             "/timesheet?v=late", alone=True, big=True, sel=[("/timesheet?v=late", ".lt-card.lt-new")], tile=['Опоздание'])
        # ⚠ не отметились (уход / приход) — сообщить бухгалтеру или директору, время впишут вручную
        _add(out, "/timesheet", "orange", len(mine["warn"]), "вы не отметились — сообщите бухгалтеру или директору",
             "/timesheet?v=warn", alone=True, shape="warn", sel=[("/timesheet?v=warn", ".lt-card.lt-warn")], tile=['Нет отметки'])
        # 🟠 опозданий за эту неделю (принятые к сведению) — горит до конца недели
        _add(out, "/timesheet", "orange", len(mine["week"]), "опозданий на этой неделе", "/timesheet?v=late",
             alone=True, tile=['Опозданий за неделю'])
        # 🟠 задержался после работы — написать, что делал (руководство решит про премию)
        _add(out, "/timesheet", "orange", len(mine["over"]), "вы задержались — напишите, что делали",
             "/timesheet?v=over", alone=True, sel=[("/timesheet?v=over", ".lt-card.lt-over")], tile=['Задержались'])
        # 24.11.2026 (брат, вечер): руководству. Директор / зам / админ — 🔴 решить (опоздание, неявка, ранний уход,
        # задержка от часа, не отметил уход), 🟣 задержка 30–59 мин. Бухгалтер — 🔴 «не отметил уход» (вписать время),
        # остальное 🟣 «ждёт решения директора» (для сведения)
        if ctx.can("attendance.alerts", "attendance.decide"):
            boss = late_svc.for_boss(db, bid, me)
            if ctx.can("attendance.decide"):
                # 25.11.2026 (брат): только то, что этот руководитель может решить (о директоре — только админ)
                red = [x for x in boss if x["level"] == "red" and x["can"] and not x["owner"]]
                own = [x for x in boss if x["level"] == "red" and x["can"] and x["owner"]]
                info = [x for x in boss if x["level"] == "info" and x["can"]]
                _add(out, "/timesheet", "red", len(own), "опоздание / неявка директора — решает только админ",
                     "/timesheet?v=boss", alone=True, owner=True, sel=[("/timesheet?v=boss", '.lt-card.lt-boss.rim-red')],
                     tile=['Нужно решить'])
                _add(out, "/timesheet", "red", len(red), "опоздания, неявки, ранний уход, задержка — решить",
                     "/timesheet?v=boss", alone=True, sel=[("/timesheet?v=boss", '.lt-card.lt-boss.rim-red')], tile=['Нужно решить'])
                _add(out, "/timesheet", "purple", len(info), "задержались после работы на 30–59 мин", "/timesheet?v=info",
                     alone=True, info=True, sel=[("/timesheet?v=info", '.lt-card.lt-boss.rim-purple')], tile=['Для сведения'])
            else:
                no_out = [x for x in boss if x["f"].kind == "no_out"]
                wait = [x for x in boss if x["f"].kind != "no_out"]
                _add(out, "/timesheet", "red", len(no_out), "не отметил уход — вписать время или принять к сведению",
                     "/timesheet?v=boss", alone=True, sel=[("/timesheet?v=boss", '.lt-card.lt-boss[data-k="no_out"]')], tile=['Нужно решить'])
                _add(out, "/timesheet", "purple", len(wait), "ждёт решения директора (опоздания, неявки, задержки)",
                     "/timesheet?v=boss", alone=True, info=True, tile=['Нужно решить', 'Для сведения'])
        # 25.11.2026 (брат): менеджер (видит табель, но время не вписывает) — 🔴 «напомните отметить уход», пока не напомнил
        elif ctx.can("attendance.view_all"):
            rem = late_svc.reminded(db, bid)
            todo = [f for f in late_svc.no_out_open(db, bid) if (f.user.id, f.day) not in rem and f.user.id != me.id]
            _add(out, "/timesheet", "red", len(todo), "не отметили уход — напомните сотрудникам", "/timesheet?v=open",
                 alone=True, sel=[("/timesheet?v=open", "tr.ts-open-row")], tile=['Забыли отметить уход'])
    return _compact(out)
