"""Мойка (18.11.2026, брат): прайс, быстрая приёмка, живая очередь с ожидаемым временем.

Быстрый наряд мойки (WorkOrder.kind="wash"): клиент, машина, класс машины, услуги из прайса (сразу работы с ценой),
кто моет (необязательно) и бокс (необязательно). Без диагностики, согласования, запчастей и подъёмника. Наряд сразу
«В работе» → мойщик «Начал / Выполнено» → «Готово» → оплата → «Выдать авто» (как у всех: только после полной оплаты).

Очередь: машины, которые ещё не начали мыть, — по времени приезда; менеджер может поднять машину «↑» (queue_rank).
Ожидаемое время: каждый бокс — своя дорожка; машина без бокса встаёт туда, где освободится раньше. Длительность —
из прайса (WoWork.minutes)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import User, Vehicle, WorkOrder, WoWork, WoWorkSession
from ..models.orders import WO_ACTIVE
from ..models.wash import WashBox, WashService
from .orders import activity_label

CLASSES = {"car": "Легковая", "suv": "Кроссовер", "bus": "Бус"}
KINDS = {"wash": "Мойка", "detail": "Детейлинг"}

# Прайс по умолчанию — обычные услуги моек; цены примерные, менеджер правит в «Мойка → Прайс»
DEFAULTS = [
    # название, вид, легковая, кроссовер, бус, минут
    ("Экспресс-мойка (поверхностная)", "wash", 200, 250, 300, 15),
    ("Мойка кузова (бесконтактная)", "wash", 250, 300, 400, 20),
    ("Мойка кузова ручная", "wash", 350, 400, 500, 30),
    ("Комплексная мойка (кузов + салон)", "wash", 600, 700, 900, 60),
    ("Пылесос салона", "wash", 200, 250, 300, 20),
    ("Влажная уборка салона", "wash", 250, 300, 350, 25),
    ("Стёкла изнутри", "wash", 100, 100, 150, 10),
    ("Мойка дисков", "wash", 150, 150, 200, 15),
    ("Чернение шин", "wash", 100, 100, 150, 10),
    ("Чернение дисков", "wash", 200, 200, 250, 20),
    ("Мойка ковриков", "wash", 50, 50, 80, 5),
    ("Мойка двигателя", "wash", 400, 450, 500, 30),
    ("Быстрый воск", "wash", 200, 250, 300, 15),
    ("Химчистка салона", "detail", 3000, 3500, 4500, 240),
    ("Полировка кузова", "detail", 4000, 5000, 6000, 360),
    ("Озонирование салона", "detail", 500, 500, 600, 30),
]


def ensure_defaults(db: Session, branch_id) -> None:
    """Прайс пустой (первый запуск) — заводим обычные услуги. Если хоть одна услуга есть — ничего не трогаем."""
    if db.scalar(select(WashService.id).where(WashService.branch_id == branch_id).limit(1)):
        return
    from ..db import current_branch_id
    current_branch_id.set(branch_id)
    for i, (name, kind, a, b, c, m) in enumerate(DEFAULTS):
        db.add(WashService(branch_id=branch_id, name=name, kind=kind, price_car=Decimal(a), price_suv=Decimal(b),
                           price_bus=Decimal(c), minutes=m, sort=(i + 1) * 10))
    db.flush()


def services(db: Session, branch_id, active_only: bool = False) -> list[WashService]:
    ensure_defaults(db, branch_id)
    q = select(WashService).where(WashService.branch_id == branch_id)
    if active_only:
        q = q.where(WashService.active.is_(True))
    return db.scalars(q.order_by(WashService.active.desc(), WashService.kind.desc(), WashService.sort, WashService.name)).all()


def price(s: WashService, car_class: str) -> Decimal:
    return Decimal({"suv": s.price_suv, "bus": s.price_bus}.get(car_class, s.price_car) or 0)


def boxes(db: Session, branch_id, active_only: bool = True) -> list[WashBox]:
    q = select(WashBox).where(WashBox.branch_id == branch_id)
    if active_only:
        q = q.where(WashBox.active.is_(True))
    return db.scalars(q.order_by(WashBox.number)).all()


def fmt_min(m: int) -> str:
    m = int(m or 0)
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч" + (f" {m % 60:02d} мин" if m % 60 else "")


# ---------- Очередь ----------
@dataclass
class Car:
    o: WorkOrder
    v: Vehicle | None
    works: list
    stage: str                       # queue — ждёт, doing — моют, done — готово (ждёт оплаты / клиента)
    minutes: int = 0                 # всего по прайсу
    left: int = 0                    # осталось (примерно)
    box: WashBox | None = None
    who: list = field(default_factory=list)
    eta_start: datetime | None = None
    eta_end: datetime | None = None
    pos: int = 0                     # место в очереди (1 — следующая)
    act: str = ""                    # «изм. 14:32 · Литвиненко» — последнее действие
    repair: bool = False             # машина с ремонта (мойка — работа в большом наряде)
    blocked: str = ""                # мыть пока нельзя — почему (серая в очереди)
    since: datetime | None = None    # с какого времени ждёт мойки (порядок, если менеджер не расставил)
    asked: object = None             # запрос мойщика «Изменить очередь», ждёт менеджера
    kinds: dict = field(default_factory=dict)    # id работы → "wash" / "detail" (по услуге прайса), 20.11.2026
    owner_role: str = ""             # должность того, кто взял машину (o.mechanic_id)


# 20.11.2026 (брат): мойщик и детейлер — разные рабочие места. Услуга без исполнителя достаётся тому, чей это вид:
# мойка — мойщику, детейлинг — детейлеру. Раньше любой брал любую, и цифры «никто не взял» у обоих были общие.
ROLE_KIND = {"washer": "wash", "detailer": "detail"}


def _role(user) -> str:
    return user.role.code if getattr(user, "role", None) else ""


def fits(user, kind: str) -> bool:
    """Подходит ли услуга этого вида человеку (вид не известен — подходит всем мойщикам)."""
    uk = ROLE_KIND.get(_role(user))
    return not kind or not uk or kind == uk


def _free_for(user, c: "Car", w) -> bool:
    """Несделанная услуга без исполнителя, которую может взять этот человек."""
    if w.status == "done" or w.mechanic_id or not fits(user, c.kinds.get(w.id, "")):
        return False
    owner = None if c.repair else c.o.mechanic_id
    if owner in (None, user.id):
        return True
    # машину взял человек другого рабочего места (мойщик) — услуга детейлера свободна для детейлера
    return ROLE_KIND.get(c.owner_role) != ROLE_KIND.get(_role(user))


def free_works(c: "Car", user) -> list:
    return [w for w in c.works if _free_for(user, c, w)]


def _stage(o: WorkOrder, works) -> str:
    if o.status in ("ready", "paid") or (works and all(w.status == "done" for w in works)):
        return "done"
    if any(w.status in ("doing", "paused", "done") for w in works):
        return "doing"
    return "queue"


def _sort_key(c: "Car"):
    """Порядок очереди: как расставил менеджер (queue_rank); остальные — сначала те, что можно мыть, по времени
    ожидания; машины, которые мыть пока нельзя, — в конце."""
    o = c.o
    return (o.queue_rank if o.queue_rank is not None else 10 ** 9, 1 if c.blocked else 0, c.since or o.created_at)


# Почему машину с ремонта мыть пока нельзя (19.11.2026, брат: серая в очереди, мойщик её не берёт)
BLOCK_STATUS = {"created": "ещё не начали — приёмка", "diag": "идёт диагностика", "approve": "не согласовано с клиентом",
                "picking": "подбирают запчасти", "waiting": "ждёт запчасти", "instock": "запчасти пришли — ремонт не начат"}


def washer_ids(db: Session, branch_id) -> set:
    """Все мойщики и детейлеры (и уволенные — их работы в старых нарядах тоже мойка)."""
    from ..models import Role
    from ..permissions import WASHERS
    return set(db.scalars(select(User.id).join(Role, User.role_id == Role.id).where(
        User.branch_id == branch_id, Role.code.in_(list(WASHERS)))))


def _blocked(db: Session, o: WorkOrder, mine: list, others: list) -> str:
    """Машина с ремонта: можно ли её уже мыть. '' — можно, иначе причина."""
    from .orders import can_start_work
    if all(can_start_work(db, o, w) for w in mine):
        if o.wash_now or all(w.status == "done" for w in others):
            return ""
        return "идёт ремонт — мыть после слесарей"
    return BLOCK_STATUS.get(o.status, "идёт ремонт")


def board(db: Session, branch_id) -> dict:
    """Всё для экрана очереди (19.11.2026: ОДНА очередь — быстрая мойка и машины с ремонта, где есть работа мойщика /
    детейлера): queue — ждут (по порядку, с «≈ начнут / ≈ готово»; серые — мыть пока нельзя), doing — моют,
    done — готовы (только быстрая мойка: машину с ремонта после мойки выдаёт менеджер вместе с ремонтом)."""
    wids = washer_ids(db, branch_id)
    from sqlalchemy import or_
    orders = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE), or_(
        WorkOrder.kind == "wash",
        WorkOrder.mechanic_id.in_(wids),
        WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id.in_(wids)))))).all() if wids else \
        db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.status.in_(WO_ACTIVE),
                                           WorkOrder.kind == "wash")).all()
    ids = [o.id for o in orders]
    all_works = {}
    for w in db.scalars(select(WoWork).where(WoWork.work_order_id.in_(ids)).order_by(WoWork.created_at)) if ids else []:
        all_works.setdefault(w.work_order_id, []).append(w)
    works, others, repair = {}, {}, set()
    for o in orders:
        ws = all_works.get(o.id, [])
        if is_wash_order(o):
            works[o.id] = ws
        else:   # с ремонта: в очереди — только работы мойщика / детейлера
            repair.add(o.id)
            works[o.id] = [w for w in ws if (w.mechanic_id or o.mechanic_id) in wids]
            others[o.id] = [w for w in ws if (w.mechanic_id or o.mechanic_id) not in wids]
    from ..models import WashQueueRequest
    asks = {r.work_order_id: r for r in db.scalars(select(WashQueueRequest).where(
        WashQueueRequest.branch_id == branch_id, WashQueueRequest.status == "new").order_by(WashQueueRequest.created_at))} \
        if ids else {}
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders])))} if ids else {}
    all_boxes = {b.id: b for b in boxes(db, branch_id, active_only=False)}
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == branch_id))}
    open_s = {}
    for s in db.scalars(select(WoWorkSession).where(WoWorkSession.work_order_id.in_(ids), WoWorkSession.ended_at.is_(None),
                                                    WoWorkSession.kind != "pause")) if ids else []:
        open_s[s.wo_work_id] = s
    t = now()
    svc_kind = {s.id: s.kind for s in db.scalars(select(WashService).where(WashService.branch_id == branch_id))}
    cars = []
    for o in orders:
        ws = works.get(o.id, [])
        total = sum(int(w.minutes or 0) for w in ws)
        left = 0
        for w in ws:
            if w.status == "done":
                continue
            m = int(w.minutes or 0)
            s = open_s.get(w.id)
            if w.status == "doing" and s and s.started_at:
                from .attendance import _aware
                m = max(m - int((t - _aware(s.started_at)).total_seconds() // 60), 5 if m else 0)
            left += m
        rep = o.id in repair
        if rep and not ws:
            continue
        who_ids = list(dict.fromkeys(i for i in [*(w.mechanic_id or (o.mechanic_id if rep else None) for w in ws),
                                                  None if rep else o.mechanic_id] if i))
        stage = _stage(o, ws)
        if rep and stage == "done":
            continue   # помыли — дальше машина у слесарей / менеджера, в очереди мойки её больше нет
        c = Car(o=o, v=vehicles.get(o.vehicle_id), works=ws, stage=stage, minutes=total, left=left,
                box=all_boxes.get(o.wash_box_id), who=[people[i] for i in who_ids if i in people],
                act=activity_label(o, people.get(o.activity_by)), repair=rep, asked=asks.get(o.id),
                kinds={w.id: svc_kind.get(w.wash_service_id, "") for w in ws},
                owner_role=_role(people.get(o.mechanic_id)) if o.mechanic_id in people else "")
        if rep:
            oth = others.get(o.id, [])
            c.blocked = _blocked(db, o, ws, oth) if stage == "queue" else ""
            c.since = max([w.done_at for w in oth if w.done_at] + [min(w.created_at for w in ws)])
        else:
            c.since = o.created_at
        cars.append(c)
    queue = sorted([c for c in cars if c.stage == "queue"], key=_sort_key)
    # 18.11.2026: «Моют» и «Готовы» — свежие сверху (последнее действие); очередь — по приезду (дата приёмки)
    from .orders import fresh_key
    doing = sorted([c for c in cars if c.stage == "doing"], key=lambda c: fresh_key(c.o), reverse=True)
    done = sorted([c for c in cars if c.stage == "done"], key=lambda c: fresh_key(c.o), reverse=True)
    # 20.11.2026 (брат): время считаем по рабочим местам — мойщики моют своё, детейлеры делают своё, друг друга не ждут
    # (раньше одна общая очередь: полировка «ждала», пока домоют все машины, — «готово ≈ 04:46»).
    # Дорожка = человек этой должности (никого нет — одна дорожка). Время машины — по её услугам каждого вида.
    from ..models import Role
    staff = {k: 0 for k in ROLE_KIND.values()}
    for code in db.scalars(select(Role.code).join(User, User.role_id == Role.id).where(
            User.branch_id == branch_id, User.active.is_(True), Role.code.in_(list(ROLE_KIND)))):
        staff[ROLE_KIND[code]] += 1
    lanes = {k: [t] * max(1, n) for k, n in staff.items()}

    def parts_of(c: Car, left_only: bool) -> dict:
        """Минуты машины по видам работ (вид не известен — мойка)."""
        out = {}
        for w in c.works:
            if left_only and w.status == "done":
                continue
            k = c.kinds.get(w.id) or "wash"
            out[k] = out.get(k, 0) + int(w.minutes or 0)
        return out

    def book(kind: str, start, minutes: int):
        ls = lanes.setdefault(kind, [t])
        i = min(range(len(ls)), key=lambda j: ls[j])
        s = max(ls[i], start)
        ls[i] = s + timedelta(minutes=minutes)
        return s, ls[i]

    for c in doing:
        c.eta_end = t + timedelta(minutes=c.left)
        rest = parts_of(c, True)   # моют сейчас — дорожка занята на ОСТАВШЕЕСЯ время (c.left), по видам пропорционально
        total = sum(rest.values())
        for k, m in rest.items():
            book(k, t, round(c.left * m / total) if total else 0)
    for i, c in enumerate(queue, 1):
        c.pos = i
        if c.blocked:
            continue   # мыть пока нельзя — время не считаем, дорожку не занимает
        starts, ends = [], []
        for k, m in sorted(parts_of(c, False).items(), key=lambda kv: kv[0] != "wash"):   # сначала мойка, потом детейлинг
            s, e = book(k, max(ends) if ends else t, m)
            starts.append(s)
            ends.append(e)
        c.eta_start = min(starts) if starts else t
        c.eta_end = max(ends) if ends else t
    return {"queue": queue, "doing": doing, "done": done, "boxes": [b for b in all_boxes.values() if b.active],
            "n_lanes": len([b for b in all_boxes.values() if b.active]) or 1}   # подпись «боксов: N» на экране


MOVES = {"up": "выше", "down": "ниже", "first": "первой", "last": "в конец"}


def move(db: Session, branch_id, wo: WorkOrder, where: str) -> bool:
    """Менеджер двигает машину в очереди (19.11.2026, брат): up / down — на одно место, first — первой (вне очереди),
    last — в конец. Порядок всей очереди запоминается (queue_rank 1…N): новые машины встают в конец."""
    order = [c.o for c in board(db, branch_id)["queue"]]
    if wo not in order or where not in MOVES:
        return False
    i = order.index(wo)
    j = {"up": i - 1, "down": i + 1, "first": 0, "last": len(order) - 1}[where]
    if j < 0 or j >= len(order) or j == i:
        return False
    order.insert(j, order.pop(i))
    for n, o in enumerate(order, 1):
        o.queue_rank = n
    return True


def place_after_ready(db: Session, branch_id, wo: WorkOrder) -> None:
    """«Можно мыть сейчас»: машина встаёт за теми, кого уже можно мыть (перед серыми), — а не вперёд всех по времени
    ожидания. Дальше менеджер двигает как обычно."""
    q = board(db, branch_id)["queue"]
    order = [c.o for c in q if c.o.id != wo.id]
    if len(order) == len(q):
        return
    ready = [i for i, c in enumerate(c for c in q if c.o.id != wo.id) if not c.blocked]
    order.insert(ready[-1] + 1 if ready else 0, wo)
    for n, o in enumerate(order, 1):
        o.queue_rank = n


def raise_up(db: Session, branch_id, wo: WorkOrder) -> bool:
    """«↑» — поднять машину на одно место в очереди (клиент спешит, постоянный)."""
    return move(db, branch_id, wo, "up")


def can_take(user: User, c: Car) -> bool:
    """Мойщик может взять машину: мыть уже можно и на неё никто не назначен или назначен он сам."""
    if c.blocked:
        return False
    # 20.11.2026: своя несделанная услуга или свободная услуга своего вида (мойка — мойщику, детейлинг — детейлеру)
    return any(w.status != "done" and w in own_works(c, user) for w in c.works) or bool(free_works(c, user))


def take(db: Session, wo: WorkOrder, user: User) -> WoWork | None:
    """Мойщик взял машину: он — исполнитель (своих несделанных работ без исполнителя), первая работа — «Начал».
    Машина с ремонта — только его работы мойки (работы слесарей не трогаем)."""
    rep = not is_wash_order(wo)
    if not wo.mechanic_id:
        wo.mechanic_id = user.id
    first = None
    kinds = {s.id: s.kind for s in db.scalars(select(WashService).where(WashService.branch_id == wo.branch_id))}
    for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)):
        mine_kind = fits(user, kinds.get(w.wash_service_id, ""))   # 20.11.2026: полировку мойщику не записываем
        if w.status != "done" and not w.mechanic_id and not rep and mine_kind:
            w.mechanic_id = user.id
        if first is None and w.status in ("todo", "paused") and (
                w.mechanic_id == user.id or (not w.mechanic_id and wo.mechanic_id == user.id and mine_kind)):
            first = w
    if first:
        t = now()
        for s in db.scalars(select(WoWorkSession).where(WoWorkSession.wo_work_id == first.id, WoWorkSession.ended_at.is_(None))):
            s.ended_at = t
        db.add(WoWorkSession(work_order_id=wo.id, wo_work_id=first.id, mechanic_id=user.id, lift_id=None, started_at=t))
        first.status = "doing"
    wo.queue_rank = None
    close_asks(db, wo)
    from .orders import event
    event(db, wo, "status", f"Мойка: взял {user.full_name}")
    return first


def next_for(db: Session, branch_id, user: User, brd: dict | None = None) -> WorkOrder | None:
    """Следующая машина для мойщика: первая в очереди, которую он может взять (серые и чужие пропускаем).
    Брать можно ТОЛЬКО её (19.11.2026, брат); другую — через запрос менеджеру «Изменить очередь»."""
    brd = brd or board(db, branch_id)
    # 19.11.2026: машину уже моют, а моя услуга (полировка у детейлера) ещё не начата — она для меня следующая
    for c in brd["doing"]:
        if not c.blocked and not started_by(c, user) and (any(w.status == "todo" for w in own_works(c, user))
                                                          or free_works(c, user)):
            return c.o
    for c in brd["queue"]:
        if can_take(user, c):
            return c.o
    return None


def own_works(c: Car, user: User) -> list:
    """Услуги этого мойщика / детейлера в машине (у быстрой мойки без исполнителя — того, кто взял машину)."""
    return [w for w in c.works if w.mechanic_id == user.id
            or (not w.mechanic_id and c.o.mechanic_id == user.id and fits(user, c.kinds.get(w.id, "")))]


def started_by(c: Car, user: User) -> bool:
    """Мойщик уже моет эту машину (взял её — есть начатая / сделанная его услуга)."""
    return any(w.status in ("doing", "paused", "done") for w in own_works(c, user))


def busy_with(c: Car, user: User) -> bool:
    """«Я мою сейчас»: начал и ещё не закончил свои услуги в этой машине."""
    return started_by(c, user) and any(w.status != "done" for w in own_works(c, user))


def start_error(db: Session, branch_id, wo: WorkOrder, user: User) -> str:
    """Мойщик жмёт «Начал» в наряде: можно, если машину он уже моет или она первая для него в очереди. Иначе — почему."""
    brd = board(db, branch_id)
    c = next((c for c in brd["doing"] + brd["queue"] if c.o.id == wo.id), None)
    if not c or c.stage == "doing" and started_by(c, user):
        return ""
    if c.blocked:
        return f"Мыть пока нельзя: {c.blocked}"
    nxt = next_for(db, branch_id, user, brd)
    if nxt and nxt.id != wo.id:
        return "Эта машина не первая в очереди. Возьмите первую или попросите менеджера изменить очередь"
    return ""


# ---------- Запрос мойщика «Изменить очередь» (19.11.2026, брат) ----------
def open_asks(db: Session, branch_id, brd: dict | None = None) -> list[Car]:
    """Машины очереди, по которым мойщик просит изменить очередь и менеджер ещё не ответил (c.asked — сам запрос)."""
    return sorted([c for c in (brd or board(db, branch_id))["queue"] if c.asked], key=lambda c: c.asked.created_at)


def ask(db: Session, wo: WorkOrder, user: User, reason: str) -> str:
    """Мойщик просит поставить машину первой. Ошибка или ''. Менеджеру наряда — сообщение (менеджерам, если не назначен)."""
    from ..models import WashQueueRequest
    from . import notify
    c = next((c for c in board(db, wo.branch_id)["queue"] if c.o.id == wo.id), None)
    if not c:
        return "Машины уже нет в очереди"
    if c.blocked:
        return f"Мыть пока нельзя: {c.blocked}"
    if c.asked:
        return "Запрос по этой машине уже отправлен — ждёт менеджера"
    if not reason.strip():
        return "Напишите, почему эту машину нужно помыть раньше"
    db.add(WashQueueRequest(work_order_id=wo.id, reason=reason.strip()[:500]))
    to = [wo.manager_id] if wo.manager_id else [u.id for u in _managers(db, wo.branch_id)]
    notify.send(db, to, "wash_queue", "Мойщик просит изменить очередь",
                f"{notify.car(db, wo)} — помыть раньше · {user.short_name}: {reason.strip()[:200]}", "/wash?tab=asksq", wo,
                skip=user.id)
    from .orders import event
    event(db, wo, "status", f"Мойка: {user.full_name} просит поставить машину первой — {reason.strip()[:200]}")
    return ""


def _managers(db: Session, branch_id) -> list:
    from .orders import staff_by_role
    return staff_by_role(db, branch_id, "manager")


def decide(db: Session, r, user: User, ok: bool, answer: str = "") -> str:
    """Менеджер: «Согласен» — машина встаёт первой; «Отказать» — с причиной. Мойщику — ответ сообщением."""
    from . import notify
    if r.status != "new":
        return "Запрос уже решён"
    wo = db.get(WorkOrder, r.work_order_id)
    if not ok and not answer.strip():
        return "Напишите причину отказа — мойщик её увидит"
    if ok and not move(db, wo.branch_id, wo, "first"):
        c = next((c for c in board(db, wo.branch_id)["queue"] if c.o.id == wo.id), None)
        if not c:
            r.status = "cancelled"
            return "Машины уже нет в очереди — запрос снят"
    r.status, r.decided_by, r.decided_at, r.answer = ("ok" if ok else "declined"), user.id, now(), answer.strip()[:500]
    if ok:
        notify.send(db, [r.created_by], "wash_queue_ok", "Очередь изменена — машина первая",
                    f"{notify.car(db, wo)} · согласовал {user.short_name}", "/wash/queue", wo)
    else:
        notify.send(db, [r.created_by], "wash_queue_no", "Отказано: очередь не меняем",
                    f"{notify.car(db, wo)} · {user.short_name}: {answer.strip()[:200]}", "/wash/queue", wo)
    from .orders import event
    event(db, wo, "status", "Мойка: " + ("машину поставили первой по запросу мойщика" if ok
                                         else f"отказано в изменении очереди — {answer.strip()[:200]}"))
    return ""


def close_asks(db: Session, wo: WorkOrder):
    """Машину взяли / выдали — открытые запросы по ней больше не нужны."""
    from ..models import WashQueueRequest
    for r in db.scalars(select(WashQueueRequest).where(WashQueueRequest.work_order_id == wo.id,
                                                       WashQueueRequest.status == "new")):
        r.status = "cancelled"


def is_wash_order(wo: WorkOrder) -> bool:
    return (wo.kind or "") == "wash"
