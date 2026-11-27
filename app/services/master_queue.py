"""Очередь мастеров и прогноз готовности (27.11.2026, брат).

Очередь формирует менеджер (он знает, кому срочно, кому не горит), а также директор, зам, админ (`schedule.queue`):
экран «Наряды → Очередь мастеров» — колонка на мастера (слесари, электрики, мотористы; у мойки своя очередь),
↑ ↓ «Первым» «В конец». Порядок — `MasterQueue` (тот же, что стрелки в «Записи»), раскладка по часам — `workload.plan`.

Прогноз «≈ готово к …» — по «Время на выполнение» (реальное время мастера, `WoWork.minutes`); не вписано — по
нормо-часам с пометкой «≈ время не вписано». Нормо-часы — только деньги. Готово = когда закончит последний мастер
наряда (с учётом рабочих часов, выходных, записей клиентов, уже идущей работы, дня привоза запчастей).

Мастер: блок «Моя очередь» в «Нарядах», «Следующая». Режим у мастера (меняют директор, зам, админ): «Подсказка»
(по умолчанию — можно начать любую) или «Обязательная» (только первую; другую — «Попросить первой», как мойщик).
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MasterQueue, MasterQueueRequest, User, WorkOrder, WoWork
from . import settings as st
from . import workload as wl

ROLES = ("mechanic", "electrician", "motorist")       # мойщик / детейлер — своя очередь мойки
KINDS = {"all": "Все", "mechanic": "Слесари", "electrician": "Электрики", "motorist": "Мотористы"}
KEY = "master_queue"


def masters(db: Session, bid, kind: str = "all") -> list[User]:
    from .orders import staff_by_role
    roles = ROLES if kind not in ROLES else (kind,)
    return sorted(staff_by_role(db, bid, list(roles)), key=lambda u: (ROLES.index(u.role.code), u.full_name))


# ---------- Режим «Подсказка / Обязательная» ----------
def strict(db: Session, bid, uid) -> bool:
    return str(uid) in (st.get(db, bid, KEY).get("strict") or [])


def set_strict(db: Session, bid, uid, on: bool) -> None:
    ids = [i for i in (st.get(db, bid, KEY).get("strict") or []) if i != str(uid)]
    if on:
        ids.append(str(uid))
    st.put(db, bid, KEY, {"strict": ids})


# ---------- Раскладка ----------
def _doing(b) -> bool:
    return b.kind == "solid" and b.rank and b.rank[0] == 0


def build(db: Session, bid, kind: str = "all", ms: list | None = None) -> dict:
    """{"masters": [...], "cols": {mid: {...}}, "orders": {wo_id: {...}}}.
    Колонка: doing — сейчас делает; queue — очередь (на складе / в работе); wait — ждут запчасти (в графике);
    off — ждут запчасти без даты / далеко; maybe — диагностика, согласование, подбор (серые)."""
    ms = ms if ms is not None else masters(db, bid, kind)
    p = wl.plan(db, bid, ms)
    cols, orders = {}, {}
    for m in ms:
        lst = p["blocks"].get(m.id, [])
        col = {"m": m, "doing": [], "queue": [], "wait": [], "off": [], "maybe": [], "strict": strict(db, bid, m.id)}
        n = 0
        for b in lst:
            b.end = b.segs[-1][1] if b.segs else None
            b.pos = None
            if b.kind != "solid":
                col["maybe"].append(b)
            elif _doing(b):
                b.pos = 0
                col["doing"].append(b)
            else:
                n += 1
                b.pos = n
                (col["wait"] if b.wo.status == "waiting" else col["queue"]).append(b)
            _merge(orders, b, m)
        cols[m.id] = col
    for b in p["off"]:
        if b.mech_id in cols:
            b.end, b.pos = None, None
            cols[b.mech_id]["off"].append(b)
            m = cols[b.mech_id]["m"]
            _merge(orders, b, m)
    for info in orders.values():
        info["label"] = ready_label(info)
        info["where"] = where_label(info)
    return {"masters": ms, "cols": cols, "orders": orders}


def _merge(orders: dict, b, m) -> None:
    i = orders.setdefault(b.wo.id, {"end": None, "off": False, "tent": False, "minutes": 0, "exact": True,
                                    "pos": [], "expected": None})
    i["minutes"] += b.minutes
    i["exact"] = i["exact"] and b.exact
    if b.kind == "off":
        i["off"] = True
        i["expected"] = b.expected
    elif b.kind != "solid":
        i["tent"] = True
    if b.end and (i["end"] is None or b.end > i["end"]):
        i["end"] = b.end
    if b.kind == "solid" or b.kind == "off":
        i["pos"].append((m, b.pos))


def fmt_min(mins: int) -> str:
    h, m = divmod(int(mins or 0), 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def when_word(dt) -> str:
    today = date.today()
    d = dt.date()
    if d == today:
        w = "сегодня"
    elif d == today + timedelta(days=1):
        w = "завтра"
    else:
        w = dt.strftime("%d.%m")
    return f"{w} к {dt:%H:%M}"


def ready_label(i: dict) -> str:
    """«≈ готово сегодня к 16:30» / «после привоза запчастей» / «≈ 3 ч работы после согласования»."""
    if i["off"]:
        return "готово после привоза запчастей" + (f" (~{i['expected']:%d.%m})" if i.get("expected") else "")
    if i["tent"]:
        return f"≈ {fmt_min(i['minutes'])} работы после согласования"
    if i["end"]:
        return "≈ готово " + when_word(i["end"])
    return ""


ORDINAL = ("", "первая", "вторая", "третья", "четвёртая", "пятая", "шестая", "седьмая", "восьмая", "девятая", "десятая")


def ordinal(n: int) -> str:
    """27.11.2026 (брат): место словами — «первая», «вторая»…, дальше «11-я»."""
    return ORDINAL[n] if 0 < n < len(ORDINAL) else f"{n}-я"


def where_label(i: dict) -> str:
    """«первая в очереди у Демченко» / «сейчас в работе у Тараса», несколько мастеров — через запятую."""
    parts = []
    for m, pos in i["pos"]:
        if pos == 0:
            parts.append(f"сейчас в работе у {m.short_name}")
        elif pos:
            parts.append(f"{ordinal(pos)} в очереди у {m.short_name}")
    out = ", ".join(parts)
    return out[:1].upper() + out[1:]


def info_for(db: Session, bid, wo_ids) -> dict:
    """Прогноз и место в очереди для плиток нарядов (одна раскладка на всех)."""
    if not wo_ids:
        return {}
    data = build(db, bid)["orders"]
    return {k: v for k, v in data.items() if k in set(wo_ids)}


# ---------- «Следующая» у мастера ----------
def next_for(db: Session, bid, user, col: dict | None = None, startable: bool = False):
    """«Следующая» у мастера: первая машина его очереди (на складе / в работе). startable — первая, которую можно
    начать прямо сейчас (запчасти выданы и «Получил»); так проверяем режим «Обязательная»."""
    from .orders import can_start_work
    from .receipt import start_blocked
    if col is None:
        col = build(db, bid, ms=[user])["cols"].get(user.id)
    if not col:
        return None
    if not startable:
        return (col["queue"] or [None])[0]
    for b in col["queue"] + col["wait"]:
        for w in db.scalars(select(WoWork).where(WoWork.work_order_id == b.wo.id, WoWork.status != "done")):
            if (w.mechanic_id or b.wo.mechanic_id) == user.id and can_start_work(db, b.wo, w) and not start_blocked(db, b.wo, w):
                return b
    return None


def start_error(db: Session, bid, wo: WorkOrder, user) -> str:
    """Режим «Обязательная»: «Начал» — только у первой машины очереди (или у машины, которую он уже делает)."""
    if user.role.code not in ROLES or not strict(db, bid, user.id):
        return ""
    col = build(db, bid, ms=[user])["cols"].get(user.id)
    if not col or any(b.wo.id == wo.id for b in col["doing"]):
        return ""
    nxt = next_for(db, bid, user, col, startable=True)
    if nxt and nxt.wo.id != wo.id:
        return (f"У вас обязательная очередь: сначала наряд {nxt.wo.number[-6:]}. "
                "Нужна эта машина — «Попросить первой» в «Моей очереди»")
    return ""


# ---------- Двигать очередь ----------
def move(db: Session, bid, mid, wo_id, where: str) -> bool:
    """up / down / first / last — среди «сплошных» (можно делать + ждут запчасти). Пишем место всей очереди мастера."""
    m = db.get(User, mid)
    q = wl.queue_of(wl.plan(db, bid, [m]), mid)
    solid = [b for b in q if b.kind == "solid"]
    rest = [b for b in q if b.kind != "solid"]
    blk = next((b for b in solid if b.wo.id == wo_id), None)
    if not blk:
        return False
    i = solid.index(blk)
    if where == "up" and i > 0:
        solid[i - 1], solid[i] = solid[i], solid[i - 1]
    elif where == "down" and i < len(solid) - 1:
        solid[i + 1], solid[i] = solid[i], solid[i + 1]
    elif where == "first":
        solid.insert(0, solid.pop(i))
    elif where == "last":
        solid.append(solid.pop(i))
    else:
        return False
    rows = {r.work_order_id: r for r in db.scalars(select(MasterQueue).where(
        MasterQueue.branch_id == bid, MasterQueue.mechanic_id == mid))}
    for n, b in enumerate(solid + rest, 1):
        r = rows.get(b.wo.id)
        if r is None:
            db.add(MasterQueue(mechanic_id=mid, work_order_id=b.wo.id, rank=n))
        elif r.rank != n:
            r.rank = n
    db.flush()
    return True


def move_and_tell(db: Session, bid, mid, wo_id, where: str, by) -> bool:
    """Двигаем; сменилась «Следующая» у мастера — ему окошко."""
    from . import notify
    m = db.get(User, mid)
    before = next_for(db, bid, m)
    if not move(db, bid, mid, wo_id, where):
        return False
    after = next_for(db, bid, m)
    if after and (not before or before.wo.id != after.wo.id):
        notify.send(db, [mid], "queue_next", "Очередь изменена — следующая машина другая",
                    f"{notify.car(db, after.wo)} · поставил {by.short_name}", "/orders#myq", after.wo, skip=by.id)
    return True


# ---------- «Попросить первой» (режим «Обязательная») ----------
def open_asks(db: Session, bid) -> list:
    return db.scalars(select(MasterQueueRequest).where(MasterQueueRequest.branch_id == bid,
                                                       MasterQueueRequest.status == "new")
                      .order_by(MasterQueueRequest.created_at)).all()


def ask(db: Session, wo: WorkOrder, user, reason: str) -> str:
    from . import notify
    if not reason.strip():
        return "Напишите, почему нужно взять эту машину раньше"
    if db.scalar(select(MasterQueueRequest.id).where(MasterQueueRequest.work_order_id == wo.id,
                                                     MasterQueueRequest.mechanic_id == user.id,
                                                     MasterQueueRequest.status == "new")):
        return "Запрос по этой машине уже отправлен — ждёт менеджера"
    db.add(MasterQueueRequest(work_order_id=wo.id, mechanic_id=user.id, reason=reason.strip()[:500]))
    from .orders import staff_by_role
    to = [wo.manager_id] if wo.manager_id else [u.id for u in staff_by_role(db, wo.branch_id, "manager")]
    notify.send(db, to, "queue_ask", "Мастер просит взять машину первой",
                f"{notify.car(db, wo)} · {user.short_name}: {reason.strip()[:200]}", "/orders/queue#asks", wo, skip=user.id)
    from .orders import event
    event(db, wo, "status", f"Очередь: {user.full_name} просит взять машину первой — {reason.strip()[:200]}")
    return ""


def decide(db: Session, r: MasterQueueRequest, user, ok: bool, answer: str = "") -> str:
    from . import notify
    from ..db import now
    if r.status != "new":
        return "Запрос уже решён"
    if not ok and not answer.strip():
        return "Напишите причину отказа — мастер её увидит"
    wo = db.get(WorkOrder, r.work_order_id)
    if ok:
        move(db, wo.branch_id, r.mechanic_id, wo.id, "first")
    r.status, r.decided_by, r.decided_at, r.answer = ("ok" if ok else "declined"), user.id, now(), answer.strip()[:500]
    if ok:
        notify.send(db, [r.mechanic_id], "queue_ok", "Очередь изменена — машина первая",
                    f"{notify.car(db, wo)} · согласовал {user.short_name}", "/orders#myq", wo)
    else:
        notify.send(db, [r.mechanic_id], "queue_no", "Отказано: очередь не меняем",
                    f"{notify.car(db, wo)} · {user.short_name}: {answer.strip()[:200]}", "/orders#myq", wo)
    from .orders import event
    event(db, wo, "status", "Очередь: " + ("машину поставили первой по просьбе мастера" if ok
                                           else f"отказано мастеру — {answer.strip()[:200]}"))
    return ""
