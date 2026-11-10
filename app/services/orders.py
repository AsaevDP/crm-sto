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


def _hour_rate(db: Session, branch_id):
    from decimal import Decimal
    from . import settings as st
    return Decimal(str(st.get(db, branch_id, "pricing")["hour_rate"]))


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
    if lift and lift.branch_id != bid:
        lift = None
    fuel = _int(f.fuel_level)
    mechanic = staff(f.mechanic_id, "mechanic") or (lift.mechanic_id if lift else None)
    wo = WorkOrder(
        number=numbering.next_number(db, bid), client_id=client.id, vehicle_id=vehicle.id,
        status="created", scenario="A" if f.scenario == "A" else "B", manager_id=ctx.user.id,
        partsman_id=staff(f.partsman_id, "partsman"), mechanic_id=mechanic,
        lift_id=lift.id if lift else None,
        mileage_in=mileage, complaint=f.complaint.strip(),
        fuel_level=fuel if fuel in FUEL_LEVELS else None,
        intake_items=[i for i in f.intake_items if i in INTAKE_ITEMS], intake_note=f.intake_note.strip(),
    )
    db.add(wo)
    db.flush()
    db.add(WoStatusHistory(work_order_id=wo.id, from_status="", to_status="created", comment="Приёмка"))
    event(db, wo, "order", f"Приёмка. {wo.complaint or ''}".strip())
    if wo.scenario == "A":
        rate = _hour_rate(db, bid)
        for name in f.work_lines()[:50]:
            db.add(WoWork(work_order_id=wo.id, name=name[:200], mechanic_id=wo.mechanic_id, pricing="hours", rate=rate))
    return wo


def route_after_intake(db: Session, wo: WorkOrder):
    """Сразу после приёмки: А → подбор запчастей, Б → диагностика (если назначен слесарь)."""
    if wo.scenario == "A" and wo.partsman_id:
        set_status(db, wo, "picking", "Сценарий А: без диагностики")
    elif wo.scenario == "B" and wo.mechanic_id:
        set_status(db, wo, "diag", "Сценарий Б: на диагностику")


# ---------- Шаги наряда и «что сделать сейчас» (11.11.2026) ----------
STEPS = [("Приём", "blue"), ("Проблемы", "purple"), ("Согласование", "yellow"), ("Запчасти", "orange"),
         ("Ремонт", "teal"), ("Выдача", "green")]
STEP_OF = {"created": 0, "diag": 1, "approve": 2, "picking": 3, "waiting": 3, "instock": 3, "work": 4,
           "ready": 5, "paid": 5, "closed": 6}


def flow(wo: WorkOrder, mech: str = "", partsman: str = "", n_find: int = 0, n_works: int = 0, n_done: int = 0,
         options_open: int = 0) -> dict:
    """Полоса шагов наверху наряда и подсказка: что сейчас сделать и кому."""
    cur = STEP_OF.get(wo.status, -1)
    skip = {1, 2} if wo.scenario == "A" and not n_find else set()
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
    elif s == "approve":
        title, text, color = ("Согласуйте с клиентом", "Позвоните клиенту: по каждой проблеме — ✓ Да или ✕ Нет. Тут же добавьте "
                              "запчасти и варианты разных фирм на выбор.", "yellow")
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


def finish_approval(db: Session, wo: WorkOrder) -> str:
    """Менеджер закончил согласование. Возвращает текст ошибки или ''.
    Согласованное → запчастисту в подбор, отказы → рекомендации в карту клиента."""
    from ..models import WoFinding
    from ..models.orders import URGENCY
    findings = db.scalars(select(WoFinding).where(WoFinding.work_order_id == wo.id)).all()
    if any(f.approval == "pending" for f in findings):
        return "Отметьте по каждой проблеме: согласовано или отказ"
    yes = [f for f in findings if f.approval == "yes"]
    if yes and not wo.partsman_id:
        return "Назначьте запчастиста — согласованное уйдёт ему в подбор"
    for f in findings:
        if f.approval == "no":
            event(db, wo, "recommendation", f.text, finding=f, urgency=f.urgency)
    # Согласованные проблемы становятся работами наряда (цены — на шаге 6)
    have = {w.finding_id for w in db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id))}
    rate = _hour_rate(db, wo.branch_id)
    for f in yes:
        if f.id not in have:
            db.add(WoWork(work_order_id=wo.id, finding_id=f.id, name=f.text[:200], mechanic_id=wo.mechanic_id,
                          pricing="hours", rate=rate))
    summary = f"Согласовано: {len(yes)}, отказ: {len(findings) - len(yes)}"
    event(db, wo, "approval", summary)
    if yes:
        set_status(db, wo, "picking", summary)
    else:
        set_status(db, wo, "ready", summary + ". Ремонт не нужен или клиент отказался")
    return ""


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
