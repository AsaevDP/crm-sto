"""Наряды: создание при приёмке, видимость по ролям, фото, поиск клиентов."""
import re
import secrets
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import DATA_DIR
from ..models import Client, Lift, User, Vehicle, WoPhoto, WorkOrder, WoStatusHistory, WoWork
from ..models.orders import FUEL_LEVELS, INTAKE_ITEMS, WO_ACTIVE
from . import numbering

PHOTO_DIR = DATA_DIR / "photos"   # не в /media: фото отдаются только через проверку прав
MAX_PHOTOS = 20
MAX_PHOTO_BYTES = 8 * 1024 * 1024

# ---------- Нормализация ----------
# На укр. номерах буквы кириллические, но выглядят как латинские. Люди вводят и так, и так —
# приводим всё к кириллице, чтобы «AA1234BC» и «АА 1234 ВС» были одним номером.
_LAT2CYR = str.maketrans("ABCEHIKMOPTXY", "АВСЕНІКМОРТХУ")


def norm_plate(s: str) -> str:
    return re.sub(r"[\s\-]", "", (s or "").upper()).translate(_LAT2CYR)


def norm_phone(s: str) -> str:
    """Храним только цифры с кодом страны: 0671234567 → 380671234567."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    return d


def fmt_phone(d: str) -> str:
    if len(d) == 12 and d.startswith("380"):
        return f"+38 {d[2:5]} {d[5:8]} {d[8:10]} {d[10:]}"
    return f"+{d}" if d else ""


def fmt_plate(p: str) -> str:
    """АА1234ВС → АА 1234 ВС (стандартный укр. формат), иначе как есть."""
    m = re.fullmatch(r"([А-ЯІЇЄ]{2})(\d{4})([А-ЯІЇЄ]{2})", p or "")
    return " ".join(m.groups()) if m else (p or "")


# ---------- Видимость ----------
def access_until(db: Session, user_id):
    """До какого времени менеджеру разрешено видеть все наряды (None — не разрешено)."""
    from datetime import datetime, timezone
    from ..models import OrderAccess
    a = db.scalar(select(OrderAccess).where(OrderAccess.user_id == user_id, OrderAccess.status == "approved")
                  .order_by(OrderAccess.until.desc()))
    if a and a.until:
        until = a.until if a.until.tzinfo else a.until.replace(tzinfo=timezone.utc)
        if until > datetime.now(timezone.utc):
            return until
    return None


def sees_all_orders(ctx) -> bool:
    """Все наряды видят: админ, директор, зам, кладовщик и т.п. (orders.view_all). Менеджер (12.11.2026) — только свои,
    ко всем — если админ / директор / зам разрешили на срок."""
    cached = getattr(ctx, "_sees_all", None)
    if cached is not None:
        return cached
    res = ctx.can("orders.view_all") and (ctx.user.role.code != "manager" or access_until(ctx.db, ctx.user.id) is not None)
    try:
        ctx._sees_all = res
    except AttributeError:
        pass
    return res


# 12.11.2026 (брат): менеджер видит своих клиентов и их наряды, пока машина у нас, и ещё RECENT_DAYS дня после выдачи.
# Потом — только через поиск при приёмке или по доступу, который дали директор / зам / админ.
RECENT_DAYS = 3


def manager_limited(ctx) -> bool:
    """Менеджер без временного доступа ко всем нарядам — видит только своё."""
    return ctx.user.role.code == "manager" and not sees_all_orders(ctx)


def _cut():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)


def granted_client_ids(ctx) -> set:
    """Клиенты, к которым менеджеру дали доступ на срок (директор / зам / админ)."""
    cached = getattr(ctx, "_granted_clients", None)
    if cached is not None:
        return cached
    from datetime import datetime, timezone
    from ..models import ClientAccess
    res = set(ctx.db.scalars(select(ClientAccess.client_id).where(
        ClientAccess.user_id == ctx.user.id, ClientAccess.status == "approved", ClientAccess.client_id.is_not(None),
        ClientAccess.until > datetime.now(timezone.utc))))
    try:
        ctx._granted_clients = res
    except AttributeError:
        pass
    return res


def _mine_recent_cond(ctx):
    from sqlalchemy import and_
    cut = _cut()
    # Выданный — по дате выдачи (closed_at); отменённый — по последнему изменению (отмена — это и есть последнее изменение)
    return and_(WorkOrder.manager_id == ctx.user.id,
                or_(WorkOrder.status.in_(WO_ACTIVE),
                    and_(WorkOrder.status == "closed", WorkOrder.closed_at >= cut),
                    and_(WorkOrder.status == "cancel", WorkOrder.updated_at >= cut)))


def _recent(wo: WorkOrder) -> bool:
    from datetime import timezone
    t = wo.closed_at if wo.status == "closed" else (wo.updated_at if wo.status == "cancel" else None)
    return bool(t and (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) >= _cut())


def visible_client_ids(ctx) -> set | None:
    """None — видит всех клиентов; иначе — id клиентов, которых видит менеджер."""
    if not manager_limited(ctx):
        return None
    ids = set(ctx.db.scalars(select(WorkOrder.client_id).where(WorkOrder.branch_id == ctx.branch.id, _mine_recent_cond(ctx))))
    return ids | granted_client_ids(ctx)


def can_see_client(ctx, client_id) -> bool:
    ids = visible_client_ids(ctx)
    return ids is None or client_id in ids


def visible_orders_query(ctx):
    """Кто видит все — все; остальные — где они назначены (менеджер ведёт, слесарь, запчастист).
    Менеджер (12.11.2026): свои — пока машина у нас и 3 дня после выдачи, плюс наряды клиентов, к которым дали доступ."""
    q = select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id)
    if manager_limited(ctx):
        me = ctx.user.id
        conds = [WorkOrder.partsman_id == me, WorkOrder.mechanic_id == me, _mine_recent_cond(ctx)]
        granted = granted_client_ids(ctx)
        if granted:
            conds.append(WorkOrder.client_id.in_(granted))
        return q.where(or_(*conds))
    if not sees_all_orders(ctx):
        me = ctx.user.id
        # слесарь видит и наряды, где ему дали отдельную работу (второй слесарь)
        q = q.where(or_(WorkOrder.partsman_id == me, WorkOrder.mechanic_id == me, WorkOrder.manager_id == me,
                        WorkOrder.id.in_(select(WoWork.work_order_id).where(WoWork.mechanic_id == me))))
    return q


def work_owner(wo: WorkOrder, w: WoWork):
    """Кто отмечает работу «Начал / Выполнено» (12.11.2026): её исполнитель, а без исполнителя — ведущий мастер."""
    return w.mechanic_id or wo.mechanic_id


def can_start_work(db: Session, wo: WorkOrder, w: WoWork) -> bool:
    """Можно ли нажимать «Начал / Пауза / Выполнено» по этой работе (12.11.2026, живой прогон).
    Наряд «В работе» — да. Пока ждём запчасти ведущего мастера, второй мастер (мойщик, электрик…) может делать свою
    работу, если под неё запчасти не нужны или уже выданы."""
    from ..models import WoPart
    if wo.status == "work":
        return True
    if wo.status not in ("picking", "waiting", "instock") or not w.mechanic_id or w.mechanic_id == wo.mechanic_id:
        return False
    return db.scalar(select(WoPart.id).where(WoPart.wo_work_id == w.id, WoPart.status.in_(
        ("need", "ordered", "arrived", "reserved", "option"))).limit(1)) is None


def history_order(ctx, wo: WorkOrder) -> bool:
    """История клиента (12.11.2026, решение брата): менеджер, который сейчас работает с клиентом, видит ВСЕ его прошлые
    визиты — и у других менеджеров (выданные / отменённые наряды), и ремонты его машин при прошлых владельцах.
    Чужие открытые наряды — нет (там работает другой менеджер)."""
    if wo.status in WO_ACTIVE:
        return False
    ids = visible_client_ids(ctx)
    if ids is None or wo.client_id in ids:
        return True
    v = ctx.db.get(Vehicle, wo.vehicle_id) if wo.vehicle_id else None
    return bool(v and v.client_id in ids)


def history_only(ctx, wo: WorkOrder) -> bool:
    """Наряд открыт менеджером только как история клиента (чужой прошлый визит) — смотреть можно, деньги не трогаем."""
    return (manager_limited(ctx) and wo.manager_id != ctx.user.id and ctx.user.id not in (wo.partsman_id, wo.mechanic_id)
            and wo.client_id not in granted_client_ids(ctx))


def client_orders_query(ctx, client_id, vehicle_ids):
    """Наряды в карточке клиента: всё, что видно по обычным правилам, плюс вся история визитов (history_order)."""
    own = or_(WorkOrder.client_id == client_id, WorkOrder.vehicle_id.in_(list(vehicle_ids)))
    if not manager_limited(ctx) or not can_see_client(ctx, client_id):
        return visible_orders_query(ctx).where(own)
    seen = visible_orders_query(ctx).where(own).with_only_columns(WorkOrder.id)
    return select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id, own,
                                   or_(WorkOrder.id.in_(seen), WorkOrder.status.not_in(WO_ACTIVE)))


def can_view(ctx, wo: WorkOrder) -> bool:
    if wo.branch_id != ctx.branch.id:
        return False
    if sees_all_orders(ctx):
        return True
    if not ctx.can("orders.view_own", "orders.view_all"):
        return False
    if manager_limited(ctx):   # 12.11.2026: свой — пока у нас и 3 дня после выдачи; чужой — по доступу к клиенту
        if wo.manager_id == ctx.user.id and (wo.status in WO_ACTIVE or _recent(wo)):
            return True
        return (wo.client_id in granted_client_ids(ctx) or ctx.user.id in (wo.partsman_id, wo.mechanic_id)
                or history_order(ctx, wo))
    if ctx.user.id in (wo.partsman_id, wo.mechanic_id, wo.manager_id):
        return True
    return ctx.db.scalar(select(WoWork.id).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id == ctx.user.id)
                         .limit(1)) is not None


# ---------- Справочники для формы ----------
def staff_by_role(db: Session, branch_id, role_code) -> list[User]:
    """Сотрудники роли. role_code — код или список кодов; "masters" — все мастера (слесарь, автоэлектрик, моторист,
    мойщик, детейлер — 12.11.2026)."""
    from ..models import Role
    from ..permissions import MASTERS
    codes = list(MASTERS) if role_code == "masters" else ([role_code] if isinstance(role_code, str) else list(role_code))
    return db.scalars(select(User).join(Role, User.role_id == Role.id)
                      .where(User.branch_id == branch_id, User.active.is_(True), Role.code.in_(codes))
                      .order_by(User.full_name)).all()


def lifts_with_load(db: Session, branch_id) -> list[tuple[Lift, WorkOrder | None]]:
    """Подъёмники и что на них сейчас стоит (последний активный наряд)."""
    lifts = db.scalars(select(Lift).where(Lift.branch_id == branch_id, Lift.active.is_(True)).order_by(Lift.number)).all()
    busy = {}
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.lift_id.is_not(None),
                                                 WorkOrder.status.in_(WO_ACTIVE)).order_by(WorkOrder.created_at)):
        busy[wo.lift_id] = wo
    return [(l, busy.get(l.id)) for l in lifts]


def lift_busy(db: Session, lift: Lift | None, wo: WorkOrder | None = None) -> WorkOrder | None:
    """На подъёмнике уже стоит другая машина (активный наряд)? Одна машина — один подъёмник (отчёт тестировщика)."""
    if not lift:
        return None
    q = select(WorkOrder).where(WorkOrder.lift_id == lift.id, WorkOrder.status.in_(WO_ACTIVE))
    if wo is not None:
        q = q.where(WorkOrder.id != wo.id)
    return db.scalar(q.order_by(WorkOrder.created_at).limit(1))


def search_clients(db: Session, branch_id, q: str, limit: int = 8) -> list[dict]:
    """Поиск по телефону, госномеру, имени, VIN. Возвращает клиента и его авто."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    digits, plate = re.sub(r"\D", "", q), norm_plate(q)
    conds = [Client.full_name.ilike(f"%{q}%")]
    if len(digits) >= 3:
        conds.append(Client.phone.contains(digits))
    veh_conds = [Vehicle.plate.contains(plate), Vehicle.vin.contains(q.upper())]
    ids = set(db.scalars(select(Client.id).where(Client.branch_id == branch_id, or_(*conds)).limit(limit)))
    ids |= set(db.scalars(select(Vehicle.client_id).where(Vehicle.branch_id == branch_id, or_(*veh_conds)).limit(limit)))
    return _client_items(db, db.scalars(select(Client).where(Client.id.in_(ids)).order_by(Client.full_name).limit(limit)))


def search_clients_recent(db: Session, branch_id, limit: int = 30) -> list[dict]:
    return _client_items(db, db.scalars(select(Client).where(Client.branch_id == branch_id)
                                        .order_by(Client.updated_at.desc()).limit(limit)))


def _client_items(db: Session, clients) -> list[dict]:
    out = []
    for c in clients:
        vehicles = db.scalars(select(Vehicle).where(Vehicle.client_id == c.id).order_by(Vehicle.created_at)).all()
        out.append({
            "id": str(c.id), "name": c.full_name, "phone": c.phone, "phone_fmt": fmt_phone(c.phone),
            "vehicles": [{"id": str(v.id), "plate": v.plate, "plate_fmt": fmt_plate(v.plate), "make": v.make,
                          "model": v.model, "year": v.year, "vin": v.vin, "mileage": v.mileage} for v in vehicles],
        })
    return out


def known_makes(db: Session, branch_id) -> list[str]:
    popular = ["Toyota", "Volkswagen", "Skoda", "Renault", "Hyundai", "Kia", "BMW", "Mercedes-Benz", "Audi",
               "Nissan", "Ford", "Opel", "Mazda", "Honda", "Mitsubishi", "Chevrolet", "Peugeot", "Citroen",
               "Lexus", "Volvo", "Subaru", "Daewoo", "ЗАЗ", "ВАЗ (Lada)"]
    mine = [m for m in db.scalars(select(Vehicle.make).where(Vehicle.branch_id == branch_id).distinct()) if m]
    return sorted(set(popular) | set(mine), key=str.lower)


# ---------- Фото ----------
def _is_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_photo(db: Session, wo: WorkOrder, data: bytes, thumb: bytes | None, stage: str = "intake",
               finding_id=None) -> WoPhoto | None:
    """Сохранить фото на диск. Проверяем «по содержимому», а не по имени файла."""
    ext = _is_image(data)
    if not ext or len(data) > MAX_PHOTO_BYTES:
        return None
    folder = PHOTO_DIR / str(wo.id)
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(8)
    (folder / f"{name}.{ext}").write_bytes(data)
    thumb_rel = ""
    if thumb and (t_ext := _is_image(thumb)) and len(thumb) < 1024 * 1024:
        (folder / f"{name}_t.{t_ext}").write_bytes(thumb)
        thumb_rel = f"{wo.id}/{name}_t.{t_ext}"
    p = WoPhoto(work_order_id=wo.id, file=f"{wo.id}/{name}.{ext}", thumb=thumb_rel, stage=stage, finding_id=finding_id)
    db.add(p)
    return p


# ---------- Создание наряда ----------
@dataclass
class IntakeForm:
    client_id: str = ""
    vehicle_id: str = ""
    phone: str = ""
    name: str = ""
    plate: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    vin: str = ""
    mileage: str = ""
    scenario: str = "B"
    complaint: str = ""
    works: str = ""            # для сценария А: по одной работе на строку
    partsman_id: str = ""
    mechanic_id: str = ""
    lift_id: str = ""
    fuel_level: str = ""
    intake_items: list = field(default_factory=list)
    intake_note: str = ""

    def errors(self, n_photos: int) -> list[str]:
        e = []
        if len(norm_phone(self.phone)) < 10:
            e.append("Телефон клиента")
        if not self.name.strip():
            e.append("Имя клиента")
        if not norm_plate(self.plate):
            e.append("Госномер")
        if not self.make.strip() or not self.model.strip():
            e.append("Марка и модель")
        if n_photos < 1:
            e.append("Хотя бы одно фото")
        if _int(self.mileage) is None:
            e.append("Пробег")
        if self.scenario == "A":
            if not self.work_lines():
                e.append("Работы (сценарий А — что делаем)")
            if not self.partsman_id:
                e.append("Запчастист (сценарий А)")
        elif not self.complaint.strip():
            e.append("Жалоба клиента — что беспокоит")
        return e

    def problems(self, db: Session, branch_id) -> list[str]:
        """Проверки по базе: пробег меньше прошлого. (Занятый подъёмник — не ошибка: машина встаёт в очередь.)"""
        e = []
        v, _ = find_vehicle(db, branch_id, self)
        mileage = _int(self.mileage)
        if v and mileage is not None and v.mileage and mileage < v.mileage:
            e.append(f"Пробег {mileage:,} км меньше прошлого ({v.mileage:,} км) — проверьте цифры".replace(",", " "))
        return e

    def work_lines(self) -> list[str]:
        return [w.strip(" -•\t") for w in self.works.splitlines() if w.strip(" -•\t")]


def _uuid(v: str):
    try:
        return uuid.UUID(v) if v else None
    except ValueError:
        return None


def _int(v: str):
    try:
        return int(str(v).replace(" ", "")) if str(v).strip() else None
    except ValueError:
        return None


def _hour_rate(db: Session, branch_id):
    from decimal import Decimal
    from . import settings as st
    return Decimal(str(st.get(db, branch_id, "pricing")["hour_rate"]))


def set_status(db: Session, wo: WorkOrder, new: str, comment: str = ""):
    db.add(WoStatusHistory(work_order_id=wo.id, from_status=wo.status if wo.status != new else "", to_status=new,
                           comment=comment))
    if new == "approve" and wo.status != "approve":
        wo.picked_at, wo.picked_by = None, None   # 13.11.2026: снова на согласование — подбор заново
    wo.status = new


def find_vehicle(db: Session, bid, f: IntakeForm) -> tuple[Vehicle | None, bool]:
    """Какая это машина. Возвращает (машина, сменился ли госномер у той же машины).
    Правила (отчёт тестировщика 11.11.2026):
      • выбрали машину и вписали другой номер → это другая машина, у старой номер НЕ меняем
        (исключение: тот же VIN — машину перерегистрировали, номер обновляем и пишем в историю);
      • VIN — главный признак машины: одинаковый VIN = та же машина, даже у другого владельца;
      • тот же госномер в базе (у кого угодно) = та же машина."""
    plate, vin = norm_plate(f.plate), (f.vin or "").strip().upper()[:32]
    chosen = db.get(Vehicle, _uuid(f.vehicle_id)) if f.vehicle_id and _uuid(f.vehicle_id) else None
    if chosen and chosen.branch_id != bid:
        chosen = None
    if chosen and chosen.plate == plate:
        return chosen, False
    if chosen and vin and chosen.vin == vin:
        return chosen, True
    if vin:
        v = db.scalar(select(Vehicle).where(Vehicle.branch_id == bid, Vehicle.vin == vin).order_by(Vehicle.updated_at.desc()))
        if v:
            return v, v.plate != plate
    v = db.scalar(select(Vehicle).where(Vehicle.branch_id == bid, Vehicle.plate == plate).order_by(Vehicle.updated_at.desc()))
    return v, False


def create_order(db: Session, ctx, f: IntakeForm) -> WorkOrder:
    bid = ctx.branch.id
    # Клиент: выбран из поиска или новый (если такой телефон уже есть — берём его).
    # Имя существующего клиента при приёмке НЕ меняем — для этого «Изменить» в карточке клиента.
    phone = norm_phone(f.phone)
    client = db.get(Client, _uuid(f.client_id)) if f.client_id and _uuid(f.client_id) else None
    if client is None or client.branch_id != bid or client.phone != phone:
        client = db.scalar(select(Client).where(Client.branch_id == bid, Client.phone == phone))
    if client is None:
        client = Client(full_name=f.name.strip()[:160], phone=phone)
        db.add(client)
        db.flush()

    plate = norm_plate(f.plate)
    vehicle, replated = find_vehicle(db, bid, f)
    if vehicle is not None and vehicle.client_id != client.id:
        # Смена владельца: машина (и вся её история ремонтов) переходит новому владельцу
        old = db.get(Client, vehicle.client_id)
        event(db, None, "note", f"Автомобиль {fmt_plate(vehicle.plate)} перешёл другому владельцу: {client.full_name}",
              client_id=vehicle.client_id, vehicle_id=vehicle.id)
        vehicle.client_id = client.id
        db.flush()
        event(db, None, "note", f"Автомобиль {fmt_plate(vehicle.plate)} — от прошлого владельца"
                                  f"{': ' + old.full_name if old else ''}. История ремонтов — в карточке",
              client_id=client.id, vehicle_id=vehicle.id)
    if vehicle is None:
        vehicle = Vehicle(client_id=client.id, plate=plate, make=f.make.strip()[:60], model=f.model.strip()[:60])
        db.add(vehicle)
    elif replated:
        event(db, None, "note", f"Госномер изменён: {fmt_plate(vehicle.plate)} → {fmt_plate(plate)}",
              client_id=client.id, vehicle_id=vehicle.id)
        vehicle.plate = plate
    # У известной машины дополняем только пустое (правка — через «Изменить» в карточке клиента)
    vehicle.make = vehicle.make or f.make.strip()[:60]
    vehicle.model = vehicle.model or f.model.strip()[:60]
    vehicle.year = vehicle.year or _int(f.year)
    vehicle.vin = vehicle.vin or (f.vin.strip().upper()[:32])
    mileage = _int(f.mileage)
    if mileage and mileage >= (vehicle.mileage or 0):
        vehicle.mileage = mileage
    db.flush()

    def staff(v, role):
        u = db.get(User, _uuid(v)) if v else None
        from ..permissions import MASTERS
        ok = u and u.branch_id == bid and u.active and (u.role.code in MASTERS if role == "masters" else u.role.code == role)
        return u.id if ok else None

    lift = db.get(Lift, _uuid(f.lift_id)) if f.lift_id and _uuid(f.lift_id) else None
    if lift and lift.branch_id != bid:
        lift = None
    # Подъёмник занят — машину всё равно принимаем, она ждёт в очереди (решение брата 11.11.2026).
    # На подъёмник её ставят, когда он освободится («Кто и где» в наряде). Слесарь поста — тот же.
    queue_for = lift if lift and lift_busy(db, lift) else None
    fuel = _int(f.fuel_level)
    mechanic = staff(f.mechanic_id, "masters") or (lift.mechanic_id if lift else None)
    wo = WorkOrder(
        number=numbering.next_number(db, bid), client_id=client.id, vehicle_id=vehicle.id,
        status="created", scenario="A" if f.scenario == "A" else "B", manager_id=ctx.user.id,
        partsman_id=staff(f.partsman_id, "partsman"), mechanic_id=mechanic,
        lift_id=lift.id if lift and not queue_for else None,
        mileage_in=mileage, complaint=f.complaint.strip(),
        fuel_level=fuel if fuel in FUEL_LEVELS else None,
        intake_items=[i for i in f.intake_items if i in INTAKE_ITEMS], intake_note=f.intake_note.strip(),
    )
    db.add(wo)
    db.flush()
    db.add(WoStatusHistory(work_order_id=wo.id, from_status="", to_status="created",
                           comment=f"Приёмка · в очереди на подъёмник {queue_for.number}" if queue_for else "Приёмка"))
    wo.queued_for = queue_for.number if queue_for else None   # для сообщения после приёмки (в базу не пишется)
    event(db, wo, "order", f"Приёмка. {wo.complaint or ''}".strip())
    if wo.scenario == "A":
        rate = _hour_rate(db, bid)
        for name in f.work_lines()[:50]:
            db.add(WoWork(work_order_id=wo.id, name=name[:200], mechanic_id=wo.mechanic_id, pricing="hours", rate=rate))
    return wo


def route_after_intake(db: Session, wo: WorkOrder):
    """Сразу после приёмки: А → согласование (запчастист подбирает, менеджер ставит цены работ, потом звонит клиенту),
    Б → диагностика (если назначен слесарь)."""
    if wo.scenario == "A" and wo.partsman_id:
        set_status(db, wo, "approve", "Сценарий А: без диагностики — подбор и согласование")
    elif wo.scenario == "B" and wo.mechanic_id:
        set_status(db, wo, "diag", "Сценарий Б: на диагностику")


def paired_partsman(db: Session, manager_id, partsmen: list):
    """Запчастист «в паре» с менеджером (11.11.2026). Если их несколько — у кого меньше открытых нарядов."""
    mine = [p for p in partsmen if p.works_with_id == manager_id]
    if not mine:
        return None
    from sqlalchemy import func as _f
    load = dict(db.execute(select(WorkOrder.partsman_id, _f.count()).where(
        WorkOrder.partsman_id.in_([p.id for p in mine]), WorkOrder.status.in_(WO_ACTIVE)).group_by(WorkOrder.partsman_id)).all())
    return min(mine, key=lambda p: (load.get(p.id, 0), p.full_name))


# ---------- Шаги наряда и «что сделать сейчас» (11.11.2026) ----------
STEPS = [("Приём", "blue"), ("Проблемы", "purple"), ("Согласование", "yellow"), ("Запчасти", "orange"),
         ("Ремонт", "teal"), ("Выдача", "green")]
STEP_OF = {"created": 0, "diag": 1, "approve": 2, "picking": 3, "waiting": 3, "instock": 3, "work": 4,
           "ready": 5, "paid": 5, "closed": 6}


def flow(wo: WorkOrder, mech: str = "", partsman: str = "", n_find: int = 0, n_works: int = 0, n_done: int = 0,
         options_open: int = 0) -> dict:
    """Полоса шагов наверху наряда и подсказка: что сейчас сделать и кому."""
    cur = STEP_OF.get(wo.status, -1)
    skip = {1} if wo.scenario == "A" and not n_find else set()   # 13.11.2026: согласование есть и в сценарии А
    steps = [{"n": i + 1, "label": label, "color": color,
              "state": "skip" if i in skip else ("done" if i < cur else ("now" if i == cur else "todo"))}
             for i, (label, color) in enumerate(STEPS)]
    mech, partsman = mech or "слесарь", partsman or "запчастист"
    s = wo.status
    if s == "cancel":
        title, text, color = "Наряд отменён", wo.cancel_reason or "", "red"
    elif s == "created" and wo.scenario == "B":
        title, text, color = ("Что с машиной?", "Запишите проблемы со слов клиента — или отправьте машину слесарю на диагностику.",
                              "purple")
    elif s == "created":
        title, text, color = "Назначьте запчастиста", "Работы уже известны (без диагностики) — запчастист подберёт запчасти.", "blue"
    elif s == "diag":
        title, text, color = (f"Диагностика · {mech}", "Слесарь записывает проблемы с фото. Закончит — «Диагностика готова», "
                              "и наряд придёт менеджеру на согласование.", "purple")
    elif s == "approve" and not wo.picked_at and wo.partsman_id:
        title, text, color = (f"Подбор · {partsman} и работы · менеджер",
                              f"{partsman.capitalize()} подбирает запчасти (со склада, заказать, варианты фирм), менеджер "
                              "составляет работы и цены. Закончит подбор — «Подбор завершён», и можно звонить клиенту.", "yellow")
    elif s == "approve":
        title, text, color = ("Согласуйте с клиентом", "Подбор готов. Позвоните клиенту: проблемы — ✓ Да / ✕ Нет, варианты "
                              "фирм — «Выбрать», лишнее — в архив. Затем «Согласовал с клиентом» — запчасти уйдут в заказ.",
                              "yellow")
    elif s == "picking" and options_open:
        title, text, color = ("Клиент выбирает запчасти", f"Ждут выбора: {options_open}. Отметьте вариант клиента кнопкой «Выбрать».",
                              "orange")
    elif s == "picking":
        title, text, color = (f"Подбор запчастей · {partsman}", "Со склада или под заказ. Закончит — «Подбор завершён».", "orange")
    elif s == "waiting":
        title, text, color = "Ждём запчасти от поставщика", "Придут на склад — наряд сам перейдёт дальше.", "orange"
    elif s == "instock":
        title, text, color = "Запчасти на складе", f"{mech.capitalize()} просит выдачу и начинает ремонт.", "teal"
    elif s == "work":
        title, text, color = f"Ремонт · {mech}", f"Выполнено работ: {n_done} из {n_works}.", "teal"
    elif s == "ready":
        title, text, color = "Машина готова", "Позвоните клиенту, примите оплату и выдайте машину.", "green"
    elif s == "paid":
        title, text, color = "Оплачено — выдайте машину", "Заполните акт выдачи в блоке «Деньги и выдача».", "green"
    else:
        title = "Выдан клиенту"
        text = wo.closed_at.astimezone().strftime("%d.%m.%Y %H:%M") if wo.closed_at else ""
        color = "green"
    tab = "main" if cur <= 2 else ("diag" if cur <= 4 else "hist")
    return {"steps": steps, "cur": cur, "title": title, "text": text, "color": color, "tab": tab}


def reopen(db: Session, wo: WorkOrder) -> str:
    """Открыть заново закрытый / отменённый наряд (по одобренному запросу). Возвращает новый статус.
    Выданный: → «Готово» — можно добавить работы, оформить возврат, снова выдать; невыплаченная зарплата по нему
    снимается и начислится заново при оплате. Отменённый: → туда, где остановились (запчасти подбираются заново)."""
    from ..models import WoPart
    from ..models.orders import PART_OFF
    if wo.status == "closed":
        from . import payroll
        payroll.cancel_for_order(db, wo)
        to = "ready"
        wo.closed_at = None
    else:
        prev = db.scalar(select(WoStatusHistory.from_status).where(WoStatusHistory.work_order_id == wo.id,
                                                                   WoStatusHistory.to_status == "cancel")
                         .order_by(WoStatusHistory.created_at.desc()).limit(1)) or "created"
        parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id, WoPart.status.notin_(PART_OFF))).all()
        works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)).all()
        if prev in ("created", "diag", "approve"):
            to = prev
        elif parts:
            to = "picking"
        elif works and all(w.status == "done" for w in works):
            to = "ready"
        elif works:
            to = "work"
        else:
            to = "created"
        wo.cancel_reason = ""
    set_status(db, wo, to, "Открыт заново по запросу")
    event(db, wo, "status", "Наряд открыт заново")
    return to


# ---------- Хронология клиента ----------
def event(db: Session, wo: WorkOrder | None, kind: str, text: str = "", client_id=None, vehicle_id=None,
          finding=None, urgency: str = ""):
    """Запись в карту клиента. Всё важное по клиенту пишется сюда (см. BRIEF 8а)."""
    from ..models import ClientEvent
    db.add(ClientEvent(client_id=client_id or wo.client_id, vehicle_id=vehicle_id or (wo.vehicle_id if wo else None),
                       work_order_id=wo.id if wo else None, finding_id=finding.id if finding else None,
                       kind=kind, text=text[:1000], urgency=urgency))


# ---------- Диагностика и согласование ----------
def add_finding(db: Session, wo: WorkOrder, text: str, urgency: str):
    from ..models import WoFinding
    from ..models.orders import URGENCY
    f = WoFinding(work_order_id=wo.id, text=text.strip()[:2000], urgency=urgency if urgency in URGENCY else "soon")
    db.add(f)
    db.flush()
    return f


def finish_diagnosis(db: Session, wo: WorkOrder, n_findings: int):
    """Слесарь закончил диагностику (или нашёл новое во время работы) → на согласование."""
    note = f"Найдено проблем: {n_findings}" if n_findings else "Проблем не найдено"
    set_status(db, wo, "approve", note)
    event(db, wo, "status", f"Диагностика завершена. {note}")


def work_for_finding(db: Session, wo: WorkOrder, f, yes: bool):
    """13.11.2026 (брат): «✓ Да» по проблеме — сразу работа в наряде (менеджер ставит цену и мастера, пока запчастист
    подбирает); «✕ Нет» — работа убирается, если её ещё не начинали."""
    w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.finding_id == f.id))
    if yes and w is None:
        db.add(WoWork(work_order_id=wo.id, finding_id=f.id, name=f.text[:200], mechanic_id=wo.mechanic_id,
                      pricing="hours", rate=_hour_rate(db, wo.branch_id)))
    elif not yes and w is not None and w.status == "todo" and not w.done_by:
        from ..models import WoPart
        for p in db.scalars(select(WoPart).where(WoPart.wo_work_id == w.id)):
            p.wo_work_id = None
        db.delete(w)
    db.flush()


def finish_approval(db: Session, wo: WorkOrder, no_parts: bool = False) -> str:
    """«Согласовал с клиентом» (13.11.2026, брат): проблемы, работы и запчасти — разом, после того как запчастист
    закончил подбор. Только теперь запчасти заказываются. Возвращает текст ошибки или ''.
    Отказы → рекомендации в карту клиента. no_parts — менеджер подтвердил «запчасти не нужны» без подбора."""
    from ..models import WoFinding, WoPart
    from ..models.orders import PART_OFF
    findings = db.scalars(select(WoFinding).where(WoFinding.work_order_id == wo.id)).all()
    if any(f.approval == "pending" for f in findings):
        return "Отметьте по каждой проблеме: согласовано или отказ"
    yes = [f for f in findings if f.approval == "yes"]
    for f in findings:
        work_for_finding(db, wo, f, f.approval == "yes")
    works = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id)).all()
    parts = db.scalars(select(WoPart).where(WoPart.work_order_id == wo.id,
                                            WoPart.status.notin_(("returned", "declined", "archived")))).all()
    summary = f"Согласовано: {len(yes)}, отказ: {len(findings) - len(yes)}"
    # Когда наряд пришёл на согласование (этот круг): новое — то, что решено и подобрано после этого
    last = db.scalar(select(WoStatusHistory).where(WoStatusHistory.work_order_id == wo.id, WoStatusHistory.to_status == "approve")
                     .order_by(WoStatusHistory.created_at.desc()).limit(1))
    prev, entered = (last.from_status, last.created_at) if last else (None, None)
    again = prev in ("work", "instock", "waiting", "picking", "ready")   # во время ремонта нашли новое
    fresh = [p for p in parts if p.status in ("need", "option") or (entered and p.created_at >= entered)]
    # 12.11.2026 (живой прогон): новая находка во время ремонта, клиент от неё отказался — машина возвращается туда,
    # где была (обычно «В работе»), а не в подбор: иначе мастер не может закончить работы.
    new_yes = [f for f in yes if again and f.approved_at and entered and f.approved_at >= entered]
    if again and not new_yes and not fresh:
        for f in findings:
            if f.approval == "no":
                event(db, wo, "recommendation", f.text, finding=f, urgency=f.urgency)
        event(db, wo, "approval", summary)
        back = "ready" if prev == "work" and works and all(w.status == "done" for w in works) else prev
        set_status(db, wo, back, summary + ". Новое не согласовано — продолжаем, где остановились")
        return ""
    if works or parts:
        if not wo.partsman_id and parts:
            return "Назначьте запчастиста — согласованное уйдёт ему в заказ"
        if wo.partsman_id and not wo.picked_at and not (no_parts and not fresh):
            return ("Запчастист ещё не закончил подбор — дождитесь «Подбор завершён»"
                    + ("" if fresh else ". Если запчасти не нужны — «Согласовал — запчасти не нужны»"))
    for f in findings:
        if f.approval == "no":
            event(db, wo, "recommendation", f.text, finding=f, urgency=f.urgency)
    event(db, wo, "approval", summary)
    if not works:
        from . import stock as ssvc
        for p in parts:   # клиент отказался от всего — подобранное в архив (резерв снимается)
            ssvc.archive_part(db, p, None)
        set_status(db, wo, "ready", summary + ". Ремонт не нужен или клиент отказался")
        return ""
    from . import stock as ssvc
    set_status(db, wo, "picking", summary + ". Согласовано с клиентом — запчасти в заказ")
    return ssvc.after_approval(db, wo)


# ---------- Выполнение работ (полоска на карточке машины) ----------
def work_progress(db: Session, order_ids) -> dict:
    """{наряд: {"done": n, "total": n, "pct": 0..100, "level": none/zero/part/all}}.
    zero — ничего не сделано (красный), part — частично (оранжевый), all — всё готово (зелёный)."""
    from sqlalchemy import func
    ids = [i for i in order_ids if i]
    out = {i: {"done": 0, "total": 0, "pct": 0, "level": "none"} for i in ids}
    if not ids:
        return out
    rows = db.execute(select(WoWork.work_order_id, WoWork.status, func.count()).where(WoWork.work_order_id.in_(ids))
                      .group_by(WoWork.work_order_id, WoWork.status)).all()
    for oid, status, n in rows:
        out[oid]["total"] += n
        if status == "done":
            out[oid]["done"] += n
    for p in out.values():
        if p["total"]:
            p["pct"] = round(100 * p["done"] / p["total"])
            p["level"] = "all" if p["done"] == p["total"] else ("part" if p["done"] else "zero")
    return out
