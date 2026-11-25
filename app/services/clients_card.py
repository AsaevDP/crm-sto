"""Одна карточка на клиента (25.11.2026, брат).

- Номер клиента «К-00001» — выдаётся сам по порядку (по филиалу), по нему ищут.
- Несколько телефонов (`ClientPhone`); основной — ещё и в `Client.phone` (документы, старый код).
- Поиск по телефону в любом виде: +380 63…, 380…, 063…, 63…, с пробелами / скобками / дефисами.
- Совпал телефон, госномер или VIN — это одна карточка. Дубли (раньше заводили по ошибке) объединяются: точные
  совпадения — сами один раз при запуске, остальное — кнопкой «Объединить» в карточке (менеджер, директор, зам, админ).
"""
import re
import uuid

from sqlalchemy import event, func, or_, select
from sqlalchemy.orm import Session

from ..models import Client, ClientEvent, ClientPhone, Vehicle, WorkOrder

_CODE = re.compile(r"^\s*[кКkK]\s*[-–—]?\s*0*(\d{1,7})\s*$")


# ---------------------------------------------------------------- телефон
def norm_phone(s: str) -> str:
    """Только цифры с кодом страны: 0671234567 / 671234567 / +38 (067) 123-45-67 → 380671234567."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    elif len(d) == 9 and not d.startswith("0"):
        d = "380" + d
    elif len(d) == 11 and d.startswith("80"):
        d = "3" + d
    return d


def phone_key(q: str) -> str:
    """Кусок для поиска «содержит»: цифры без кода страны и без ведущего 0 (+380 63 12… и 063 12… → 6312…)."""
    d = re.sub(r"\D", "", q or "")
    for p in ("380", "80"):
        if d.startswith(p) and len(d) > len(p) + 1:
            return d[len(p):]
    return d[1:] if d.startswith("0") and len(d) > 2 else d


def client_ids_by_phone(db: Session, branch_id, q: str, exact: bool = False) -> set:
    """Клиенты по телефону (все номера карточки). exact — полный номер (9+ цифр), иначе — кусок от 3 цифр."""
    if exact:
        full = norm_phone(q)
        if len(full) < 12:
            return set()
        conds_p, conds_c = [ClientPhone.phone == full], [Client.phone == full]
    else:
        k = phone_key(q)
        if len(k) < 3:
            return set()
        conds_p, conds_c = [ClientPhone.phone.contains(k)], [Client.phone.contains(k)]
    ids = set(db.scalars(select(ClientPhone.client_id).where(ClientPhone.branch_id == branch_id, *conds_p)))
    ids |= set(db.scalars(select(Client.id).where(Client.branch_id == branch_id, *conds_c)))
    return _alive(db, ids)


def by_phone(db: Session, branch_id, phone: str, exclude=None) -> Client | None:
    """Карточка по полному телефону (любой из её номеров)."""
    ids = client_ids_by_phone(db, branch_id, phone, exact=True) - ({exclude} if exclude else set())
    return db.get(Client, sorted(ids, key=str)[0]) if ids else None


def phones(db: Session, c: Client) -> list[ClientPhone]:
    ps = db.scalars(select(ClientPhone).where(ClientPhone.client_id == c.id).order_by(ClientPhone.created_at)).all()
    return sorted(ps, key=lambda p: p.phone != c.phone)   # основной — первым


def add_phone(db: Session, c: Client, phone: str, note: str = "") -> str:
    """Добавить номер в карточку. Ошибка — текст. Номер у другого клиента — ошибка (сначала объединить карточки)."""
    p = norm_phone(phone)
    if len(p) < 12:
        return "Номер телефона: 10 цифр, например 067 123 45 67"
    other = client_ids_by_phone(db, c.branch_id, p, exact=True) - {c.id}
    if other:
        o = db.get(Client, next(iter(other)))
        return f"Этот номер уже у клиента {o.full_name} ({o.code}) — если это один человек, нажмите «Объединить»"
    if not db.scalar(select(ClientPhone.id).where(ClientPhone.client_id == c.id, ClientPhone.phone == p)):
        db.add(ClientPhone(client_id=c.id, branch_id=c.branch_id, phone=p, note=note.strip()[:60]))
    if not c.phone:
        c.phone = p
    db.flush()
    return ""


# ---------------------------------------------------------------- номер клиента
def parse_code(q: str) -> int | None:
    """«К-12», «к12», «K 00012» → 12 (буква обязательна; просто «12» — см. search)."""
    m = _CODE.match(q or "")
    return int(m.group(1)) if m else None


def by_number(db: Session, branch_id, n: int) -> Client | None:
    return db.scalar(select(Client).where(Client.branch_id == branch_id, Client.number == n, Client.merged_into.is_(None)))


@event.listens_for(Session, "before_flush")
def _new_client(session, flush_context, instances):
    """Новый клиент: номер по порядку, основной телефон — и в список телефонов. Сменили основной — тоже в список."""
    nxt: dict = {}
    for obj in list(session.new):
        if not isinstance(obj, Client):
            continue
        if obj.id is None:
            obj.id = uuid.uuid4()
        bid = obj.branch_id
        if bid is None:
            from ..db import current_branch_id
            bid = obj.branch_id = current_branch_id.get()
        if not obj.number:
            if bid not in nxt:
                with session.no_autoflush:
                    nxt[bid] = (session.execute(select(func.max(Client.number)).where(Client.branch_id == bid)).scalar() or 0)
            nxt[bid] += 1
            obj.number = nxt[bid]
        if obj.phone:
            session.add(ClientPhone(client_id=obj.id, branch_id=bid, phone=obj.phone))
    for obj in list(session.dirty):
        if isinstance(obj, Client) and obj.phone:
            from sqlalchemy import inspect as _insp
            h = _insp(obj).attrs.phone.history
            if h.has_changes():
                with session.no_autoflush:
                    have = session.execute(select(ClientPhone.id).where(ClientPhone.client_id == obj.id,
                                                                        ClientPhone.phone == obj.phone)).first()
                if not have:
                    session.add(ClientPhone(client_id=obj.id, branch_id=obj.branch_id, phone=obj.phone))


# ---------------------------------------------------------------- одна карточка
def _alive(db: Session, ids) -> set:
    """Объединённые карточки — заменить на ту, куда объединили."""
    out = set()
    for i in ids:
        c = db.get(Client, i)
        seen = 0
        while c is not None and c.merged_into and seen < 5:
            c, seen = db.get(Client, c.merged_into), seen + 1
        if c is not None:
            out.add(c.id)
    return out


def alive(db: Session, c: Client | None) -> Client | None:
    if c is None:
        return None
    ids = _alive(db, {c.id})
    return db.get(Client, next(iter(ids))) if ids else c


def merge(db: Session, keep: Client, drop: Client, user=None, why: str = "") -> str:
    """Объединить drop в keep: машины, наряды, записи, доступы, история, телефоны. drop остаётся пустой со ссылкой
    merged_into (старые ссылки ведут в keep). Ошибка — текст."""
    from ..models import Appointment, ClientAccess
    if keep.id == drop.id:
        return "Это одна и та же карточка"
    if keep.branch_id != drop.branch_id:
        return "Карточки разных филиалов"
    for M_ in (Vehicle, WorkOrder, ClientEvent, ClientAccess, Appointment):
        for x in db.scalars(select(M_).where(M_.client_id == drop.id)):
            x.client_id = keep.id
    have = {p.phone for p in phones(db, keep)}
    for p in db.scalars(select(ClientPhone).where(ClientPhone.client_id == drop.id)):
        if p.phone in have:
            db.delete(p)
        else:
            p.client_id = keep.id
            have.add(p.phone)
    if drop.phone and drop.phone not in have:
        db.add(ClientPhone(client_id=keep.id, branch_id=keep.branch_id, phone=drop.phone))
    if not keep.phone:
        keep.phone = drop.phone
    if drop.notes and drop.notes not in (keep.notes or ""):
        keep.notes = ((keep.notes or "") + "\n" + drop.notes).strip()
    keep.is_regular = bool(keep.is_regular or drop.is_regular)
    drop.merged_into = keep.id
    drop.phone = ""
    who = f" · {user.short_name}" if user is not None else ""
    from .orders import event as _ev
    _ev(db, None, "note", f"Объединена карточка {drop.code} «{drop.full_name}»{(' — ' + why) if why else ''}{who}",
        client_id=keep.id)
    db.flush()
    return ""


def duplicate_groups(db: Session, branch_id) -> list[set]:
    """Точные совпадения между разными карточками: телефон, VIN (17 знаков), госномер (6+ знаков)."""
    pairs: list[set] = []
    rows = db.execute(select(ClientPhone.phone, ClientPhone.client_id).join(Client, Client.id == ClientPhone.client_id)
                      .where(ClientPhone.branch_id == branch_id, Client.merged_into.is_(None))).all()
    by: dict = {}
    for ph, cid in rows:
        if len(ph) >= 12:
            by.setdefault(("p", ph), set()).add(cid)
    for v in db.scalars(select(Vehicle).join(Client, Client.id == Vehicle.client_id)
                        .where(Vehicle.branch_id == branch_id, Client.merged_into.is_(None))):
        if v.vin and len(v.vin) == 17:
            by.setdefault(("v", v.vin), set()).add(v.client_id)
        if v.plate and len(v.plate) >= 6:
            by.setdefault(("n", v.plate), set()).add(v.client_id)
    for ids in by.values():
        if len(ids) > 1:
            pairs.append(set(ids))
    groups: list[set] = []   # склеить пересекающиеся
    for s in pairs:
        hit = [g for g in groups if g & s]
        for g in hit:
            s |= g
            groups.remove(g)
        groups.append(s)
    return groups


def fill_once(db: Session):
    """Один раз (25.11.2026): номера клиентам по дате появления, телефоны — в список, точные дубли — объединить."""
    from ..db import current_branch_id
    from ..models import Branch
    from . import settings as st
    for br in db.scalars(select(Branch)):
        if st.get(db, br.id, "grants_once").get("clients.card_v1"):
            continue
        current_branch_id.set(br.id)
        n = db.scalar(select(func.max(Client.number)).where(Client.branch_id == br.id)) or 0
        for c in db.scalars(select(Client).where(Client.branch_id == br.id, Client.number.is_(None)).order_by(Client.created_at)):
            n += 1
            c.number = n
        have = set(db.execute(select(ClientPhone.client_id, ClientPhone.phone).where(ClientPhone.branch_id == br.id)).all())
        for c in db.scalars(select(Client).where(Client.branch_id == br.id, Client.phone != "")):
            if (c.id, c.phone) not in have:
                db.add(ClientPhone(client_id=c.id, branch_id=br.id, phone=c.phone))
        db.flush()
        for g in duplicate_groups(db, br.id):
            cs = sorted((db.get(Client, i) for i in g), key=lambda c: (c.created_at, c.number or 0))
            for d in cs[1:]:
                merge(db, cs[0], d, why="совпали телефон, госномер или VIN")
        st.put(db, br.id, "grants_once", {"clients.card_v1": True})
    db.commit()


def search_ids(db: Session, branch_id, q: str) -> set:
    """Все клиенты по запросу: номер клиента (К-12), телефон (кусок от 3 цифр), имя, госномер, VIN."""
    from .orders import norm_plate
    q = (q or "").strip()
    out: set = set()
    n = parse_code(q)
    if n:
        c = by_number(db, branch_id, n)
        return {c.id} if c else set()
    out |= client_ids_by_phone(db, branch_id, q)
    out |= set(db.scalars(select(Client.id).where(Client.branch_id == branch_id, Client.merged_into.is_(None),
                                                 Client.full_name.ilike(f"%{q}%"))))
    plate = norm_plate(q)
    conds = []
    if len(plate) >= 3:
        conds.append(Vehicle.plate.contains(plate))
    if len(q) >= 4:
        conds.append(Vehicle.vin.contains(q.upper().replace(" ", "")))
    if conds:
        out |= set(db.scalars(select(Vehicle.client_id).where(Vehicle.branch_id == branch_id, or_(*conds))))
    if q.isdigit():   # просто цифры — может быть и номер клиента
        c = by_number(db, branch_id, int(q))
        if c:
            out.add(c.id)
    return _alive(db, out)
