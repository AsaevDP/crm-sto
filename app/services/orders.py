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
def visible_orders_query(ctx):
    """Менеджер/админ (orders.view_all) видят все, остальные — где они назначены."""
    q = select(WorkOrder).where(WorkOrder.branch_id == ctx.branch.id)
    if not ctx.can("orders.view_all"):
        me = ctx.user.id
        q = q.where(or_(WorkOrder.partsman_id == me, WorkOrder.mechanic_id == me, WorkOrder.manager_id == me))
    return q


def can_view(ctx, wo: WorkOrder) -> bool:
    if wo.branch_id != ctx.branch.id:
        return False
    if ctx.can("orders.view_all"):
        return True
    return ctx.can("orders.view_own") and ctx.user.id in (wo.partsman_id, wo.mechanic_id, wo.manager_id)


# ---------- Справочники для формы ----------
def staff_by_role(db: Session, branch_id, role_code: str) -> list[User]:
    from ..models import Role
    return db.scalars(select(User).join(Role, User.role_id == Role.id)
                      .where(User.branch_id == branch_id, User.active.is_(True), Role.code == role_code)
                      .order_by(User.full_name)).all()


def lifts_with_load(db: Session, branch_id) -> list[tuple[Lift, WorkOrder | None]]:
    """Подъёмники и что на них сейчас стоит (последний активный наряд)."""
    lifts = db.scalars(select(Lift).where(Lift.branch_id == branch_id, Lift.active.is_(True)).order_by(Lift.number)).all()
    busy = {}
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.lift_id.is_not(None),
                                                 WorkOrder.status.in_(WO_ACTIVE)).order_by(WorkOrder.created_at)):
        busy[wo.lift_id] = wo
    return [(l, busy.get(l.id)) for l in lifts]


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


def save_photo(db: Session, wo: WorkOrder, data: bytes, thumb: bytes | None, stage: str = "intake") -> WoPhoto | None:
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
    p = WoPhoto(work_order_id=wo.id, file=f"{wo.id}/{name}.{ext}", thumb=thumb_rel, stage=stage)
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
        if self.scenario == "A":
            if not self.work_lines():
                e.append("Работы (сценарий А — что делаем)")
            if not self.partsman_id:
                e.append("Запчастист (сценарий А)")
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


def set_status(db: Session, wo: WorkOrder, new: str, comment: str = ""):
    db.add(WoStatusHistory(work_order_id=wo.id, from_status=wo.status if wo.status != new else "", to_status=new,
                           comment=comment))
    wo.status = new


def create_order(db: Session, ctx, f: IntakeForm) -> WorkOrder:
    bid = ctx.branch.id
    # Клиент: выбран из поиска или новый (если такой телефон уже есть — берём его)
    phone = norm_phone(f.phone)
    client = db.get(Client, _uuid(f.client_id)) if f.client_id else None
    if client is None or client.branch_id != bid:
        client = db.scalar(select(Client).where(Client.branch_id == bid, Client.phone == phone))
    if client is None:
        client = Client(full_name=f.name.strip()[:160], phone=phone)
        db.add(client)
        db.flush()
    else:
        client.full_name = f.name.strip()[:160] or client.full_name

    # Авто: выбрано или новое (тот же госномер у этого клиента — то же авто)
    plate = norm_plate(f.plate)
    vehicle = db.get(Vehicle, _uuid(f.vehicle_id)) if f.vehicle_id else None
    if vehicle is None or vehicle.client_id != client.id:
        vehicle = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.plate == plate))
    if vehicle is None:
        vehicle = Vehicle(client_id=client.id)
        db.add(vehicle)
    vehicle.plate, vehicle.make, vehicle.model = plate, f.make.strip()[:60], f.model.strip()[:60]
    vehicle.year = _int(f.year) or vehicle.year
    vehicle.vin = (f.vin.strip().upper()[:32]) or vehicle.vin
    mileage = _int(f.mileage)
    if mileage:
        vehicle.mileage = mileage
    db.flush()

    def staff(v, role):
        u = db.get(User, _uuid(v)) if v else None
        return u.id if u and u.branch_id == bid and u.active and u.role.code == role else None

    lift = db.get(Lift, _uuid(f.lift_id)) if f.lift_id else None
    fuel = _int(f.fuel_level)
    wo = WorkOrder(
        number=numbering.next_number(db, bid), client_id=client.id, vehicle_id=vehicle.id,
        status="created", scenario="A" if f.scenario == "A" else "B", manager_id=ctx.user.id,
        partsman_id=staff(f.partsman_id, "partsman"), mechanic_id=staff(f.mechanic_id, "mechanic"),
        lift_id=lift.id if lift and lift.branch_id == bid else None,
        mileage_in=mileage, complaint=f.complaint.strip(),
        fuel_level=fuel if fuel in FUEL_LEVELS else None,
        intake_items=[i for i in f.intake_items if i in INTAKE_ITEMS], intake_note=f.intake_note.strip(),
    )
    db.add(wo)
    db.flush()
    db.add(WoStatusHistory(work_order_id=wo.id, from_status="", to_status="created", comment="Приёмка"))
    if wo.scenario == "A":
        for name in f.work_lines()[:50]:
            db.add(WoWork(work_order_id=wo.id, name=name[:200], mechanic_id=wo.mechanic_id))
    return wo


def route_after_intake(db: Session, wo: WorkOrder):
    """Сразу после приёмки: А → подбор запчастей, Б → диагностика (если назначен слесарь)."""
    if wo.scenario == "A" and wo.partsman_id:
        set_status(db, wo, "picking", "Сценарий А: без диагностики")
    elif wo.scenario == "B" and wo.mechanic_id:
        set_status(db, wo, "diag", "Сценарий Б: на диагностику")
