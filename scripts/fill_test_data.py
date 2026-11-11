"""ВРЕМЕННОЕ наполнение рабочей базы тестовыми данными (10.11.2026), чтобы «пощупать» CRM целиком.

Что делает:
  1) копия текущей базы → data/backup-before-test-ГГГГММДД-ЧЧММ.db;
  2) демо-набор (app/demo_data.py): наряды на всех этапах, склад, разбор, инструмент, запись, табель;
  3) история за 3 месяца: ~90 закрытых нарядов с работами, запчастями, оплатами и долгами, закупки,
     зарплата (начисления и выплаты), табель, больше клиентов — чтобы Кабинет, Деньги, Зарплата не были пустыми.
Все люди, телефоны и номера — выдуманные. Входы — как в docs/test-users.md; новые слесари: логин + «123».

Запуск (CRM на 8810 перед этим остановить):
    .venv\\Scripts\\python scripts\\fill_test_data.py
Вернуть базу как было (тоже при остановленной CRM):
    .venv\\Scripts\\python scripts\\fill_test_data.py --restore
"""
import os
import random
import shutil
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_PASSWORD", "temp-password-123")   # тут же заменим на «логин123»
os.environ.pop("DEMO", None)

from app.config import DATA_DIR  # noqa: E402

DB_FILE = DATA_DIR / "crm.db"


# ---------------------------------------------------------------- копия / возврат
def backup() -> Path:
    out = DATA_DIR / f"backup-before-test-{datetime.now():%Y%m%d-%H%M}.db"
    src, dst = sqlite3.connect(DB_FILE), sqlite3.connect(out)
    src.backup(dst)
    dst.close(), src.close()
    return out


def restore():
    copies = sorted(DATA_DIR.glob("backup-before-test-*.db"))
    if not copies:
        sys.exit("Копии нет — возвращать нечего")
    src = copies[-1]
    for suffix in ("-wal", "-shm"):
        Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
    shutil.copyfile(src, DB_FILE)
    print(f"База возвращена из {src.name}")


# ---------------------------------------------------------------- данные
NEW_CLIENTS = [
    ("0671002001", "Богдан Ярошенко", "АА 4521 КМ", "Volkswagen", "Golf VII", 2015, 176000),
    ("0501002002", "Татьяна Мельниченко", "КА 7310 ЕВ", "Toyota", "RAV4", 2016, 142000),
    ("0631002003", "Василий Онищенко", "АІ 2208 ВС", "Ford", "Focus III", 2014, 205000),
    ("0971002004", "Людмила Захарченко", "ВІ 6620 АА", "Kia", "Ceed", 2018, 97000),
    ("0681002005", "Артём Савченко", "АА 9090 ТК", "Skoda", "Superb", 2017, 188000),
    ("0991002006", "Юлия Гордиенко", "КА 3141 АР", "Hyundai", "Elantra", 2019, 81000),
    ("0661002007", "Николай Пономаренко", "СВ 5511 ОН", "Renault", "Duster", 2018, 132000),
    ("0951002008", "Оксана Демчук", "АА 1708 МО", "Nissan", "X-Trail", 2016, 158000),
    ("0731002009", "Евгений Лысенко", "ВС 2025 АІ", "Mitsubishi", "Outlander", 2017, 165000),
    ("0501002010", "Алла Кушнир", "КА 6060 НН", "Honda", "CR-V", 2015, 171000),
    ("0671002011", "Ростислав Бойко", "АІ 4747 КЕ", "Chevrolet", "Aveo", 2012, 214000),
    ("0631002012", "Галина Карпенко", "АА 3003 ВВ", "Peugeot", "308", 2016, 139000),
    ("0981002013", "Владимир Тимошенко", "ВН 8181 СА", "Audi", "A6 C7", 2014, 233000),
    ("0681002014", "Кристина Остапчук", "КА 5252 ІМ", "Volkswagen", "Tiguan", 2019, 88000),
    ("0661002015", "Станислав Нестеренко", "АХ 7007 ВР", "Lexus", "RX 350", 2016, 149000),
    ("0931002016", "Валентина Бережная", "КА 1919 АО", "Toyota", "Corolla", 2020, 61000),
]

def more_clients(n: int, rnd: random.Random):
    """Ещё n выдуманных клиентов: имя, телефон, номер, машина."""
    first = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Алексей", "Иван", "Михаил", "Юрий", "Олег",
             "Анна", "Мария", "Елена", "Ольга", "Наталья", "Ирина", "Светлана", "Виктория", "Тарас", "Богдан"]
    last = ["Шевченко", "Бондаренко", "Ткаченко", "Кравченко", "Олейник", "Коваль", "Поліщук", "Марченко", "Лысенко",
            "Руденко", "Савчук", "Мельник", "Петренко", "Клименко", "Павленко", "Левченко", "Гончаренко", "Сидоренко"]
    cars = [("Toyota", "Camry"), ("Toyota", "Corolla"), ("Volkswagen", "Passat B7"), ("Volkswagen", "Jetta"),
            ("Skoda", "Octavia A7"), ("Skoda", "Fabia"), ("Renault", "Logan"), ("Renault", "Megane IV"), ("Hyundai", "Accent"),
            ("Hyundai", "Tucson"), ("Kia", "Rio"), ("Kia", "Sportage"), ("Ford", "Fiesta"), ("Mazda", "6"), ("Nissan", "Leaf"),
            ("Chevrolet", "Lacetti"), ("Daewoo", "Lanos"), ("Opel", "Vectra C"), ("BMW", "320d F30"), ("Audi", "A4 B8")]
    regions = ["АА", "КА", "АІ", "ВІ", "ВС", "СВ", "ВН", "АХ"]
    letters = "АВСЕНІКМОРТХ"
    out = []
    for i in range(n):
        fn, ln = rnd.choice(first), rnd.choice(last)
        make, model = rnd.choice(cars)
        year = rnd.randint(2008, 2022)
        plate = f"{rnd.choice(regions)} {rnd.randint(1000, 9999)} {rnd.choice(letters)}{rnd.choice(letters)}"
        out.append((f"0{rnd.choice((50, 63, 66, 67, 68, 73, 93, 95, 96, 97, 98, 99))}{3000000 + i * 7919 % 999999:07d}",
                    f"{fn} {ln}", plate, make, model, year, (2026 - year) * rnd.randint(14000, 21000)))
    return out


EXTRA_STOCK = [  # название, артикул, бренд, закупка, категория, подкатегория, ячейка
    ("Комплект ГРМ", "CT1028K2", "Contitech", "2900", "Ремни и ролики", "ГРМ", "B / 1 / 3"),
    ("Помпа водяная", "P 7522", "Hepu", "1350", "Охлаждение", "Помпы", "B / 1 / 4"),
    ("Амортизатор передний", "334829", "Kayaba", "2100", "Ходовая и подвеска", "Амортизаторы", "B / 2 / 3"),
    ("Подшипник ступицы передний", "VKBA 3643", "SKF", "1250", "Ходовая и подвеска", "Подшипники ступиц", "B / 2 / 4"),
    ("Аккумулятор 74 Ач", "574 104 068", "Varta", "3600", "Электрика", "Аккумуляторы", "B / 3 / 2"),
    ("Лампа H7", "64210", "Osram", "180", "Щётки и лампы", "", "A / 3 / 3"),
    ("Тормозная жидкость DOT4, 1 л", "1 987 479 107", "Bosch", "260", "Жидкости", "Тормозная", "A / 2 / 3"),
    ("Ремень приводной", "6PK1070", "Gates", "520", "Ремни и ролики", "Приводные", "A / 3 / 4"),
    ("Фильтр топливный", "WK 820/17", "MANN", "650", "Фильтры", "Топливные", "A / 1 / 4"),
    ("Масло трансмиссионное 75W-90, 1 л", "Syntrans 75W-90", "Castrol", "480", "Масла", "Трансмиссионные", "A / 2 / 4"),
    ("Датчик ABS передний", "0 986 594 527", "Bosch", "1150", "Электрика", "Датчики", "B / 3 / 3"),
    ("Наконечник рулевой", "JTE1027", "TRW", "560", "Рулевое", "Наконечники", "B / 3 / 4"),
    ("Термостат", "TH6687.87J", "Wahler", "740", "Охлаждение", "Термостаты", "B / 1 / 4"),
    ("Смазка ШРУС, 400 г", "LM 47", "Liqui Moly", "210", "Расходники", "Смазки и герметики", "A / 3 / 4"),
]

JOBS = [  # вес, [(работа, часы или None, цена часа / фикс)], [(запчасть, кол-во)]
    (10, [("Замена масла двигателя", 0.6, 800), ("Замена масляного фильтра", None, 150), ("Замена воздушного фильтра", None, 150)],
     [("Масло моторное 5W-30, 4 л", 1), ("Фильтр масляный", 1), ("Фильтр воздушный", 1)]),
    (6, [("Замена передних тормозных колодок", 0.8, 800)], [("Колодки тормозные передние", 1)]),
    (3, [("Замена передних тормозных дисков и колодок", 1.5, 800)], [("Колодки тормозные передние", 1), ("Диск тормозной передний", 2)]),
    (4, [("Замена задних тормозных колодок", 0.8, 800)], [("Колодки тормозные задние", 1)]),
    (4, [("Замена свечей зажигания", 0.7, 800)], [("Свеча зажигания", 4)]),
    (4, [("Замена стоек стабилизатора", 0.8, 800)], [("Стойка стабилизатора передняя", 2)]),
    (3, [("Замена сайлентблоков переднего рычага", 2.0, 850)], [("Сайлентблок переднего рычага", 2)]),
    (2, [("Замена охлаждающей жидкости", 1.0, 800)], [("Антифриз G12+, 5 л", 1)]),
    (4, [("Замена салонного фильтра", None, 200)], [("Фильтр салона угольный", 1)]),
    (5, [("Диагностика ходовой части", None, 400)], []),
    (5, [("Развал-схождение", None, 900)], []),
    (2, [("Замена ремня ГРМ с роликами", 3.5, 900), ("Замена помпы", None, 600)], [("Комплект ГРМ", 1), ("Помпа водяная", 1)]),
    (2, [("Замена передних амортизаторов", 2.0, 850)], [("Амортизатор передний", 2)]),
    (2, [("Замена подшипника ступицы", 1.5, 850)], [("Подшипник ступицы передний", 1)]),
    (2, [("Замена аккумулятора", None, 150)], [("Аккумулятор 74 Ач", 1)]),
    (4, [("Компьютерная диагностика двигателя", None, 500)], []),
    (3, [("Замена ламп ближнего света", None, 200)], [("Лампа H7", 2)]),
    (2, [("Замена тормозной жидкости", 0.8, 800)], [("Тормозная жидкость DOT4, 1 л", 1)]),
    (2, [("Замена приводного ремня", 0.7, 800)], [("Ремень приводной", 1)]),
    (2, [("Замена топливного фильтра", 0.6, 800)], [("Фильтр топливный", 1)]),
    (1, [("Замена масла в КПП", 0.8, 800)], [("Масло трансмиссионное 75W-90, 1 л", 2)]),
    (1, [("Замена датчика ABS", 0.7, 800)], [("Датчик ABS передний", 1)]),
    (2, [("Замена рулевых наконечников", 1.0, 800)], [("Наконечник рулевой", 2)]),
    (1, [("Замена термостата", 1.2, 800)], [("Термостат", 1)]),
]
MECHS = ["andrey", "sergey", "viktor", "taras", "igor", "roman"]
SALARY = {"irina": 16000, "oleg": 12000, "pavel": 11000, "dmitro": 11000}
DAYS = 91


def history():
    """3 месяца закрытых нарядов. Каждый наряд создаётся «сейчас» обычными функциями CRM,
    затем все его строки (наряд, работы, запчасти, движения склада, оплаты, зарплата, журнал) переносятся в прошлое."""
    from sqlalchemy import Date, select, update

    from app.db import Base, SessionLocal, UTCDateTime, current_branch_id, current_user_id, now
    from app.demo_data import CLIENTS, STOCK, _photo
    from app.models import (Appointment, Branch, Client, Location, PayrollAccrual, PayrollPayout, Purchase, User,
                            Vehicle, WoPart, WorkOrder, WoWork)
    from app.models import PurchaseLine, Supplier
    from app.security import hash_secret
    from app.services import money as msvc
    from app.services import orders as osvc
    from app.services import stock as ssvc
    from app.services import numbering
    from app.services import settings as st

    rnd = random.Random(2026)
    today = date.today()
    with SessionLocal() as db:
        branch = db.scalar(select(Branch))
        current_branch_id.set(branch.id)
        users = {u.login: u for u in db.scalars(select(User))}
        ctx_cache = {}

        def as_user(login):
            u = users[login]
            current_user_id.set(u.id)
            return ctx_cache.setdefault(login, type("C", (), {"branch": branch, "user": u, "can": lambda *a: True})())

        # Пароли новых слесарей — как у остальных тестовых: логин + 123; все «работают» больше года
        for login in ("viktor", "taras", "igor", "roman", "marina", "vasyl", "denys", "yura"):
            users[login].password_hash = hash_secret(f"{login}123")
        for login, u in users.items():
            if login != "roman" and not u.hired_on:
                u.hired_on = today - timedelta(days=rnd.randint(400, 1500))
        db.flush()

        def shift(mark, day: date, hh: int, mm: int):
            """Всё созданное после mark — в прошлое: на день day, время hh:mm (местное)."""
            db.flush()
            from app.services.attendance import local_tz
            target = datetime.combine(day, time(hh, mm), tzinfo=local_tz()).astimezone(timezone.utc)
            delta = mark - target
            for t in Base.metadata.sorted_tables:
                if "created_at" not in t.c or "id" not in t.c:
                    continue
                dts = [c for c in t.c if isinstance(c.type, UTCDateTime)]
                ds = [c for c in t.c if isinstance(c.type, Date) and not isinstance(c.type, UTCDateTime)]
                for r in db.execute(select(t.c.id, *dts, *ds).where(t.c.created_at >= mark)).all():
                    m = r._mapping
                    vals = {c.name: m[c.name] - delta for c in dts if m[c.name] is not None and m[c.name] >= mark}
                    vals.update({c.name: day for c in ds if m[c.name] == today})
                    if vals:
                        db.execute(update(t).where(t.c.id == r.id).values(**vals))
            db.expire_all()

        # ---- Склад: новые позиции, три закупки (3, 2 и 1 месяц назад) ----
        locs = {l.label: l for l in db.scalars(select(Location))}
        sup = db.scalar(select(Supplier).where(Supplier.name.like("Автотехнікс%")))
        sup2 = db.scalar(select(Supplier).where(Supplier.name.like("Элит%")))
        cats, cost, cell = {}, {}, {}
        from app.models import PartCatalog
        for name, art, brand, c, qty, cl in STOCK:
            cats[name] = db.scalar(select(PartCatalog).where(PartCatalog.name == name))
            cost[name], cell[name] = Decimal(c), cl
        as_user("oleg")
        for name, art, brand, c, cat_, sub_, cl in EXTRA_STOCK:
            pc = ssvc.get_or_create_catalog(db, name, art, brand)
            pc.category, pc.subcategory = cat_, sub_
            cats[name], cost[name], cell[name] = pc, Decimal(c), cl
        db.flush()
        need, wsum = {}, sum(j[0] for j in JOBS)
        jobs_total = DAYS * 3.2 * 1.25          # ≈ машин за 3 месяца × дел на машину
        for w, _, parts in JOBS:
            for p, q in parts:
                need[p] = need.get(p, 0) + jobs_total * w / wsum * q
        for i, (days_ago, supplier, inv) in enumerate(((DAYS + 3, sup, "АТ-000871"), (60, sup2, "ЭУ-61204"), (30, sup, "АТ-001102"))):
            mark = now()
            as_user("oleg")
            pur = Purchase(supplier_id=supplier.id, status="ordered", supplier_invoice=inv)
            db.add(pur)
            db.flush()
            for name, w in need.items():
                qty = max(2, round(w * (0.45, 0.35, 0.35)[i]))
                db.add(PurchaseLine(purchase_id=pur.id, catalog_id=cats[name].id, qty=Decimal(qty), price=cost[name]))
            db.flush()
            as_user("pavel")
            for line in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == pur.id)).all():
                name = next(n for n, c in cats.items() if c and c.id == line.catalog_id)
                ssvc.receive_line(db, branch.id, line, locs[cell[name]].id)
            ssvc.refresh_purchase_status(db, pur)
            shift(mark, today - timedelta(days=days_ago), 10, 30)

        # ---- Клиенты: старые из демо ходят чаще, плюс новые ----
        people = [(c, 3 if i in (0, 1, 4, 5) else 1) for i, c in enumerate(CLIENTS) if i not in (9,)]
        people += [(c, 2 if i < 6 else 1) for i, c in enumerate(NEW_CLIENTS)]
        people += [(c, 1) for c in more_clients(60, rnd)]
        pool = [c for c, w in people for _ in range(w)]
        fired_on = users["roman"].dismissed_on or today
        orders_made, appts = 0, []
        for days_ago in range(DAYS, 0, -1):
            d = today - timedelta(days=days_ago)
            if d.weekday() == 6:
                continue
            for _ in range(rnd.choice((2, 3, 3, 4, 4, 5) if d.weekday() < 5 else (1, 2))):
                phone, name, plate, make, model, year, km = rnd.choice(pool)
                job = rnd.choices(JOBS, weights=[j[0] for j in JOBS])[0]
                if rnd.random() < 0.25:   # иногда два дела за визит
                    extra = rnd.choices(JOBS, weights=[j[0] for j in JOBS])[0]
                    if extra is not job:
                        job = (0, job[1] + extra[1], job[2] + extra[2])
                mechs = [m for m in MECHS if m != "roman" or d < fired_on]
                mech = rnd.choice(mechs)
                hh = rnd.randint(8, 15)
                mark = now()
                ctx = as_user("irina")
                f = osvc.IntakeForm(phone=phone, name=name, plate=plate, make=make, model=model, year=str(year),
                                    mileage=str(km - days_ago * 45), scenario="A", works="\n".join(w[0] for w in job[1]),
                                    partsman_id=str(users["oleg"].id) if job[2] else "", fuel_level=rnd.choice(("25", "50", "75")),
                                    intake_items=["docs"])
                wo = osvc.create_order(db, ctx, f)
                _photo(db, wo, rnd.choice(("front", "rear", "left", "right")))
                osvc.route_after_intake(db, wo)
                wo.mechanic_id = users[mech].id
                db.flush()
                ws = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all()
                for w, (_, hours, rate) in zip(ws, job[1]):
                    w.pricing = "fixed" if hours is None else "hours"
                    w.hours = None if hours is None else Decimal(str(hours))
                    w.rate = Decimal(str(rate))
                    w.price = msvc.work_price(w)
                    w.mechanic_id = users[mech].id
                db.flush()
                if job[2]:
                    as_user("oleg")
                    for pname, q in job[2]:
                        c = cats[pname]
                        markup = rnd.choice((30, 35, 40, 45, 50))
                        part = WoPart(work_order_id=wo.id, catalog_id=c.id, name=c.name, qty=Decimal(q), cost=cost[pname],
                                      markup_pct=markup, price=ssvc.sale_price(cost[pname], markup), picked_by=users["oleg"].id)
                        db.add(part)
                        db.flush()
                        if not ssvc.reserve_from_stock(db, branch.id, part):
                            db.delete(part)   # на складе кончилось — без этой детали
                            db.flush()
                    ssvc.finish_picking(db, wo)
                    if db.scalar(select(WoPart).where(WoPart.work_order_id == wo.id)):
                        as_user(mech)
                        req = ssvc.request_issue(db, wo)
                        as_user("pavel")
                        ssvc.issue(db, req, users["pavel"].id)
                as_user(mech)
                for w in ws:
                    w.status, w.done_at = "done", now()
                osvc.set_status(db, wo, "ready", "Все работы выполнены")
                as_user("irina")
                if rnd.random() < 0.12:
                    wo.discount_pct = Decimal(rnd.choice((5, 10)))
                db.flush()
                due = msvc.totals(db, wo).due
                method = rnd.choices(("cash", "card", "terminal"), weights=(5, 3, 4))[0]
                msvc.add_payment(db, wo, due, method, "full", "")   # в долг не работаем (11.11.2026)
                adv = rnd.random() < 0.2
                msvc.close(db, wo, "advice" if adv else "ok", "",
                           rnd.choice(("Задние колодки ~40 % — проверить при следующем ТО", "Шины: износ протектора, к зиме менять",
                                       "Течь масла с сальника — следить", "Аккумулятор слабый, заменить до морозов")) if adv else "")
                shift(mark, d, hh, rnd.randint(0, 59))
                orders_made += 1
                if days_ago <= 7:
                    appts.append((d, hh, mech, name, phone, f"{make} {model}", plate, job[1][0][0], wo.id))

        # ---- Запись: прошедшие визиты последних двух недель — «приехал» ----
        as_user("irina")
        from app.services.schedule import to_utc
        for d, hh, mech, name, phone, car, plate, work, wo_id in appts:
            wo = db.get(WorkOrder, wo_id)
            db.add(Appointment(start_at=to_utc(d, f"{hh:02d}:00"), duration_min=60, mechanic_id=users[mech].id,
                               client_name=name, phone=osvc.norm_phone(phone), car=car, plate=osvc.norm_plate(plate),
                               work=work, status="arrived", work_order_id=wo_id, client_id=wo.client_id,
                               vehicle_id=wo.vehicle_id))

        # ---- Табель за 2–3 месяца назад (последние два месяца заполнил демо-набор) ----
        from app.models import AccessPoint, AttendanceEvent
        from app.services.attendance import local_tz
        gate = db.scalar(select(AccessPoint).where(AccessPoint.kind == "gate"))
        demo_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        d = (demo_start - timedelta(days=62)).replace(day=1)
        workers = [u for login, u in users.items() if login != "admin" and u.role.code not in ("director", "deputy")]
        while d < demo_start:
            for u in workers:
                if u.login == "roman" and d > fired_on:
                    continue
                wd = d.weekday()
                if wd == 6 or (wd == 5 and u.role.code not in ("mechanic", "manager")) or rnd.random() < 0.05:
                    continue
                for hh, mm, direction in ((7 + (rnd.random() < 0.7), rnd.randint(30, 59), "in"),
                                          (14 if wd == 5 else 17 + (rnd.random() < 0.5), rnd.randint(0, 59), "out")):
                    at = datetime.combine(d, time(hh, mm), tzinfo=local_tz()).astimezone(timezone.utc)
                    db.add(AttendanceEvent(user_id=u.id, point_id=gate.id if gate else None, direction=direction, at=at, method="card"))
            d += timedelta(days=1)

        # ---- Зарплата: оклады офиса за прошлые месяцы и выплаты (аванс + расчёт) ----
        as_user("admin")
        m = (today.replace(day=1) - timedelta(days=DAYS)).replace(day=1)
        while m < today.replace(day=1):
            m_end = (m + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            for login, amount in SALARY.items():
                db.add(PayrollAccrual(user_id=users[login].id, base_amount=Decimal(1), rate=Decimal(amount),
                                      amount=Decimal(amount), on_date=m_end, kind="salary", note="Оклад за месяц"))
            db.flush()
            for login in list(SALARY) + MECHS:
                u = users[login]
                if login == "roman" and m <= fired_on <= m_end:
                    continue   # расчёт при увольнении уже есть в демо-наборе
                total =db.scalar(select(__import__("sqlalchemy").func.sum(PayrollAccrual.amount)).where(
                    PayrollAccrual.user_id == u.id, PayrollAccrual.status != "cancelled",
                    PayrollAccrual.on_date >= m, PayrollAccrual.on_date <= m_end)) or Decimal(0)
                if total <= 0:
                    continue
                adv = (Decimal(total) * Decimal("0.4")).quantize(Decimal(100))
                db.add(PayrollPayout(user_id=u.id, period_from=m, period_to=m_end, amount=adv,
                                     paid_on=m.replace(day=20), method="cash", note="Аванс"))
                db.add(PayrollPayout(user_id=u.id, period_from=m, period_to=m_end, amount=Decimal(total) - adv,
                                     paid_on=m_end, method="card", note="Расчёт за месяц"))
            m = m_end + timedelta(days=1)

        # ---- Постоянные клиенты — у кого 4+ визита ----
        from sqlalchemy import func
        for cid, n in db.execute(select(WorkOrder.client_id, func.count()).group_by(WorkOrder.client_id)).all():
            if n >= 4:
                db.get(Client, cid).is_regular = True

        # ---- Номера нарядов — по порядку дат ----
        db.flush()
        cfg = st.get(db, branch.id, "order_number")
        all_wo = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch.id).order_by(WorkOrder.created_at)).all()
        old = {wo.id: wo.number for wo in all_wo}
        for wo in all_wo:
            wo.number = f"tmp-{wo.id}"
        db.flush()
        renum = {}
        for i, wo in enumerate(all_wo, 1):
            wo.number = numbering.format_number(cfg["template"], branch.code, wo.created_at.year, i)
            renum[old[wo.id]] = wo.number
        db.flush()
        # Старые номера в текстах (уведомления, история, журнал, инструмент) → новые
        import re
        from sqlalchemy import String, Text
        rx = re.compile("|".join(re.escape(k) for k in sorted(renum, key=len, reverse=True)))
        for t in Base.metadata.sorted_tables:
            if "id" not in t.c or t.name == "work_orders":
                continue
            cols = [c for c in t.c if isinstance(c.type, (String, Text)) and not isinstance(c.type, UTCDateTime)]
            if not cols:
                continue
            for r in db.execute(select(t.c.id, *cols)).all():
                vals = {c.name: rx.sub(lambda m: renum[m.group(0)], r._mapping[c.name]) for c in cols
                        if isinstance(r._mapping[c.name], str) and rx.search(r._mapping[c.name])}
                if vals:
                    db.execute(update(t).where(t.c.id == r.id).values(**vals))
        db.expire_all()
        from app.models import OrderCounter
        cnt = db.scalar(select(OrderCounter).where(OrderCounter.branch_id == branch.id, OrderCounter.year == today.year))
        if cnt:
            cnt.last = len(all_wo)
        current_user_id.set(None)
        db.commit()
        print(f"История: нарядов {orders_made}, записей в календаре {len(appts)}")


def main():
    if "--restore" in sys.argv:
        restore()
        return
    if not DB_FILE.exists():
        sys.exit("Базы нет — сначала запустите CRM один раз")
    con = sqlite3.connect(DB_FILE)
    has_clients = con.execute("select count(*) from clients").fetchone()[0]
    con.close()
    if has_clients:
        sys.exit("В базе уже есть клиенты — тестовые данные кладутся только в пустую. Вернуть как было: --restore")
    b = backup()
    print(f"Копия базы: {b}")
    import app.main  # noqa: F401  — подключает журнал (audit), он же ставит branch_id / created_by
    from app.db import Base, engine, add_missing_columns
    from app.seed import seed
    Base.metadata.create_all(engine)
    add_missing_columns()
    seed()
    from app.demo_data import populate
    populate()
    history()
    print("Готово")


if __name__ == "__main__":
    main()
