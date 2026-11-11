"""Список клиентов (10.11.2026): вкладки «Все / Постоянные / Должники / Давно не были / Новые»,
сортировка по визитам, имени, марке, госномеру, сумме; фильтр по марке."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Client, Payment, Vehicle, WorkOrder
from . import orders as osvc

REGULAR_VISITS = 3      # с какого числа визитов клиент «постоянный»
LOST_DAYS = 180         # «давно не были» — больше полугода
NEW_DAYS = 30           # «новые» — первый визит за последний месяц

# «Должники» убраны 11.11.2026: СТО не работает в долг (старый долг, если есть, виден в карточке клиента)
TABS = {"all": "Все", "regular": "Постоянные", "lost": "Давно", "new": "Новые"}
SORTS = {"last": "Недавние", "name": "Имя А–Я", "make": "Марка", "plate": "Госномер",
         "visits": "Больше визитов", "spent": "Больше оплат"}


@dataclass
class Row:
    c: Client
    vehicles: list = field(default_factory=list)
    visits: int = 0
    first: datetime | None = None
    last: datetime | None = None
    spent: Decimal = Decimal(0)
    debt: Decimal = Decimal(0)
    active: int = 0              # машин сейчас на СТО

    @property
    def regular(self) -> bool:
        return bool(self.c.is_regular) or self.visits >= REGULAR_VISITS

    @property
    def make_model(self) -> str:
        return " ".join(f"{v.make} {v.model}".strip() for v in self.vehicles[:1]).lower()

    @property
    def last_local(self) -> datetime | None:
        return _local(self.last) if self.last else None

    @property
    def days_since(self) -> int | None:
        return (date.today() - _local(self.last).date()).days if self.last else None


def _local(dt: datetime) -> datetime:
    from .attendance import local_tz
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(local_tz())


def rows(db: Session, branch_id, client_id=None) -> list[Row]:
    from ..models.orders import WO_ACTIVE
    qc = select(Client).where(Client.branch_id == branch_id)
    qv = select(Vehicle).where(Vehicle.branch_id == branch_id)
    qo = select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.status != "cancel")
    if client_id:
        qc, qv, qo = qc.where(Client.id == client_id), qv.where(Vehicle.client_id == client_id), qo.where(WorkOrder.client_id == client_id)
    out = {c.id: Row(c) for c in db.scalars(qc)}
    for v in db.scalars(qv.order_by(Vehicle.created_at)):
        if v.client_id in out:
            out[v.client_id].vehicles.append(v)
    debt_orders = []
    for o in db.scalars(qo):
        r = out.get(o.client_id)
        if not r:
            continue
        r.visits += 1
        r.first = o.created_at if not r.first or o.created_at < r.first else r.first
        r.last = o.created_at if not r.last or o.created_at > r.last else r.last
        if o.status in WO_ACTIVE:
            r.active += 1
        if o.status == "closed" and o.closed_with_debt_comment:
            debt_orders.append(o)
    for client_id, s in db.execute(select(WorkOrder.client_id, func.sum(Payment.amount))
                                   .join(Payment, Payment.work_order_id == WorkOrder.id)
                                   .where(WorkOrder.branch_id == branch_id).group_by(WorkOrder.client_id)):
        if client_id in out:
            out[client_id].spent = Decimal(s or 0)
    if debt_orders:
        from . import money as msvc
        for o in debt_orders:
            due = msvc.totals(db, o).due
            if due > 0:
                out[o.client_id].debt += due
    return list(out.values())


def search_ids(db: Session, branch_id, q: str) -> set | None:
    if len((q or "").strip()) < 2:
        return None
    return {x["id"] for x in osvc.search_clients(db, branch_id, q, limit=500)}


def apply(items: list[Row], tab: str, sort: str, make: str, ids: set | None) -> list[Row]:
    if ids is not None:
        items = [r for r in items if str(r.c.id) in ids]
    if make:
        items = [r for r in items if any(v.make == make for v in r.vehicles)]
    if tab == "regular":
        items = [r for r in items if r.regular]
    elif tab == "debt":
        items = [r for r in items if r.debt > 0]
    elif tab == "lost":
        items = [r for r in items if r.days_since is not None and r.days_since > LOST_DAYS]
    elif tab == "new":
        items = [r for r in items if r.first and (date.today() - _local(r.first).date()).days <= NEW_DAYS]
    far = datetime(1970, 1, 1, tzinfo=timezone.utc)
    key = {
        "name": (lambda r: r.c.full_name.lower(), False),
        "make": (lambda r: (r.make_model or "яяя", r.c.full_name.lower()), False),
        "plate": (lambda r: (r.vehicles[0].plate if r.vehicles else "яяя"), False),
        "visits": (lambda r: (r.visits, r.last or far), True),
        "spent": (lambda r: (r.spent, r.last or far), True),
    }.get(sort, (lambda r: (r.last or (r.c.created_at if r.c.created_at.tzinfo else r.c.created_at.replace(tzinfo=timezone.utc))), True))
    return sorted(items, key=key[0], reverse=key[1])


def counts(items: list[Row]) -> dict:
    return {t: len(apply(items, t, "last", "", None)) for t in TABS}


def makes(items: list[Row]) -> list[tuple[str, int]]:
    n: dict[str, int] = {}
    for r in items:
        for m in {v.make for v in r.vehicles if v.make}:
            n[m] = n.get(m, 0) + 1
    return sorted(n.items(), key=lambda x: (-x[1], x[0]))


def one(db: Session, c: Client) -> Row:
    """Показатели одного клиента (для карточки)."""
    return rows(db, c.branch_id, client_id=c.id)[0]
