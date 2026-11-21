"""Рабочие места (21.11.2026, брат) — было «Подъёмники и слесари».

Пять видов (models.org.PLACE_KINDS): подъёмник (слесарь) до 20, электрик, моторист, мойка, детейлинг — до 5.
Номер — внутри вида: «Подъёмник 3», «Электрик 1». Все 40 мест заведены сразу, лишние выключены.
Мастер закреплён за местом; один мастер — одно место. Мастер нажал «Начал» — машина встаёт на его место (seat).
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Lift, User, WorkOrder, WoWorkSession
from ..models.org import PLACE_KINDS
from ..models.orders import WO_ACTIVE

KIND_ORDER = list(PLACE_KINDS)
ROLE_KIND = {v[2]: k for k, v in PLACE_KINDS.items()}   # должность → вид места


def kind_for_role(role_code: str) -> str | None:
    return ROLE_KIND.get(role_code)


def sort_key(l: Lift):
    return (KIND_ORDER.index(l.kind) if l.kind in KIND_ORDER else 0, l.number)


def all_places(db: Session, branch_id, active_only: bool = False) -> list[Lift]:
    q = select(Lift).where(Lift.branch_id == branch_id)
    if active_only:
        q = q.where(Lift.active.is_(True))
    return sorted(db.scalars(q).all(), key=sort_key)


def _guess_kind(l: Lift, roles: dict) -> str:
    rk = ROLE_KIND.get(roles.get(l.mechanic_id, ""))
    if rk:
        return rk
    n = (l.name or "").lower()
    for word, k in (("мойк", "wash"), ("детейл", "detail"), ("электр", "electric"), ("двигат", "motor"), ("мотор", "motor")):
        if word in n:
            return k
    return "lift"


_PREFIX = re.compile(r"^\s*пост\s*\d+\s*(·|-|—)?\s*", re.I)
_PLAIN = {"мойка", "детейлинг", "электрика", "электрик", "двигатель", "моторист", "подъёмник", ""}


def _clean_name(name: str) -> str:
    s = _PREFIX.sub("", name or "").strip()
    return "" if s.lower() in _PLAIN else s[:60]


def ensure(db: Session, branch_id) -> bool:
    """Старые посты без вида → вид по мастеру / названию, номера по порядку внутри вида; недостающие места до
    лимита — выключенные. Свободным мастерам — первое свободное место своего вида (только при переводе)."""
    rows = db.scalars(select(Lift).where(Lift.branch_id == branch_id).order_by(Lift.number)).all()
    changed = False
    old = [l for l in rows if not l.kind]
    if old:
        roles = {u.id: u.role.code for u in db.scalars(select(User).where(User.branch_id == branch_id)) if u.role}
        used = {k: {l.number for l in rows if l.kind == k} for k in PLACE_KINDS}
        for l in old:
            k = _guess_kind(l, roles)
            n = 1
            while n in used[k]:
                n += 1
            if n > PLACE_KINDS[k][3]:   # вид уже полон — в подъёмники
                k, n = "lift", max(used["lift"] | {0}) + 1
            used[k].add(n)
            l.kind, l.number, l.name = k, n, _clean_name(l.name)
        db.flush()
        changed = True
        _attach_free_masters(db, branch_id)
        db.flush()
        # Машины, с которыми мастер работает прямо сейчас, а на месте их нет (наряд 32 у Демченко) — ставим на место
        for mid, wid in working_now(db, branch_id).items():
            wo = db.get(WorkOrder, wid)
            if wo and not wo.lift_id and wo.status in WO_ACTIVE:
                seat(db, wo, mid)
    for k, (_, _, _, limit) in PLACE_KINDS.items():
        have = {l.number for l in db.scalars(select(Lift).where(Lift.branch_id == branch_id, Lift.kind == k))}
        for n in range(1, limit + 1):
            if n not in have:
                db.add(Lift(kind=k, number=n, name="", active=False, branch_id=branch_id))
                changed = True
    if changed:
        db.flush()
    return changed


def _attach_free_masters(db: Session, branch_id):
    places = all_places(db, branch_id)
    taken = {l.mechanic_id for l in places if l.mechanic_id}
    for u in db.scalars(select(User).where(User.branch_id == branch_id, User.active.is_(True)).order_by(User.full_name)):
        k = ROLE_KIND.get(u.role.code if u.role else "")
        if not k or u.id in taken:
            continue
        free = next((l for l in places if l.kind == k and l.active and not l.mechanic_id), None)
        if free:
            free.mechanic_id = u.id
            taken.add(u.id)


def place_of(db: Session, user_id) -> Lift | None:
    return db.scalar(select(Lift).where(Lift.mechanic_id == user_id, Lift.active.is_(True)).limit(1))


def working_now(db: Session, branch_id) -> dict:
    """Мастер → наряд, где у него сейчас идёт работа (открытый отрезок «работа»)."""
    out = {}
    for s in db.scalars(select(WoWorkSession).where(WoWorkSession.branch_id == branch_id, WoWorkSession.ended_at.is_(None))
                        .order_by(WoWorkSession.started_at)):
        if (s.kind or "work") == "work":
            out[s.mechanic_id] = s.work_order_id
    return out


def seat(db: Session, wo: WorkOrder, master_id) -> Lift | None:
    """«Начал» (21.11.2026, брат): машина встаёт на место этого мастера. Машина уже на чужом месте (другой мастер) —
    не двигаем, на «Пульте» место мастера и так покажет «работает с машиной». На месте стоит другая машина, с которой
    сейчас никто не работает, — она уходит в очередь."""
    place = place_of(db, master_id)
    if not place or wo.lift_id == place.id or wo.kind == "wash":
        return None
    if wo.lift_id:
        cur = db.get(Lift, wo.lift_id)
        if cur and cur.mechanic_id and cur.mechanic_id != master_id:
            return None
    busy_now = set(working_now(db, wo.branch_id).values())
    for other in db.scalars(select(WorkOrder).where(WorkOrder.lift_id == place.id, WorkOrder.status.in_(WO_ACTIVE),
                                                    WorkOrder.id != wo.id)):
        if other.id in busy_now:
            return None   # там сейчас идёт работа — машину не снимаем
        other.lift_id = None
    wo.lift_id = place.id
    return place


def save(db: Session, place: Lift, kind: str, number: int, name: str, master: User | None, active: bool) -> str:
    """Настройки: вид и номер — если такой уже есть, места меняются номерами (и видом). Мастер — только своей
    должности; был на другом месте — снимается оттуда. Возвращает текст ошибки или ""."""
    if kind not in PLACE_KINDS or not (1 <= number <= PLACE_KINDS[kind][3]):
        return "Нет такого номера"
    if master and ROLE_KIND.get(master.role.code if master.role else "") != kind:
        return f"На место «{PLACE_KINDS[kind][0]}» — только {PLACE_KINDS[kind][1].lower()} по должности"
    if (place.kind, place.number) != (kind, number):
        other = db.scalar(select(Lift).where(Lift.branch_id == place.branch_id, Lift.kind == kind, Lift.number == number,
                                             Lift.id != place.id))
        if other:
            other.kind, other.number = place.kind, place.number
            db.flush()
        place.kind, place.number = kind, number
    place.name = (name or "").strip()[:60]
    if master:
        for l in db.scalars(select(Lift).where(Lift.mechanic_id == master.id, Lift.id != place.id)):
            l.mechanic_id = None
    place.mechanic_id = master.id if master else None
    place.active = active
    return ""
