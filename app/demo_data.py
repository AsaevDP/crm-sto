"""Наполнение ДЕМО-версии выдуманными данными, чтобы было что «пощупать».

Запускается только при DEMO=1 и только на пустой базе (на Render база каждый раз новая).
Данные создаются через те же функции, что и в работе: статусы, остатки и история — настоящие.
Все люди, телефоны и номера — выдуманные.
"""
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from .config import DEMO_PASSWORD
from .db import SessionLocal, current_branch_id, current_user_id
from .models import (Branch, Lift, Location, Purchase, Role, Tool, User, WoFinding, WoPart, WorkOrder)
from .security import hash_secret
from .services import orders as osvc
from .services import money as msvc
from .services import stock as ssvc

PHOTOS = Path(__file__).parent / "demo" / "photos"

EXTRA_MECHANICS = [("viktor", "Виктор Слюсаренко"), ("taras", "Тарас Гайко"), ("igor", "Игорь Мельник"),
                   ("roman", "Роман Шевчук")]  # Роман в конце увольняется — пример для вкладки «Архив»
FIRED_DAYS_AGO = 10
EXTRA_OFFICE = [("marina", "Марина Приёмкина", "manager"), ("vasyl", "Василий Деталькин", "partsman"),
                ("denys", "Денис Подборов", "partsman"), ("yura", "Юрий Каталогов", "partsman")]
PAIRS = {"oleg": "irina", "vasyl": "irina", "denys": "marina", "yura": "marina"}   # запчастист → менеджер

CLIENTS = [
    # телефон, имя, госномер, марка, модель, год, пробег
    ("0671112233", "Олег Коваленко", "АА 1234 ВС", "Toyota", "Camry", 2018, 124500),
    ("0502223344", "Марина Петренко", "КА 5678 АІ", "Volkswagen", "Passat B8", 2017, 181200),
    ("0633334455", "Виктор Сидоренко", "АІ 9012 КК", "Skoda", "Octavia A7", 2016, 210300),
    ("0974445566", "Андрей Данилюк", "ВІ 3344 ОО", "Renault", "Megane IV", 2019, 98700),
    ("0685556677", "Елена Литвин", "АА 0001 ХХ", "BMW", "X5 F15", 2015, 167800),
    ("0996667788", "Сергей Бондар", "ВС 7777 АА", "Hyundai", "Tucson", 2021, 45100),
    ("0937778899", "Ирина Ткаченко", "КА 2468 ВВ", "Kia", "Sportage", 2020, 73400),
    ("0668889900", "Дмитрий Мороз", "АХ 1357 СЕ", "Mercedes-Benz", "E 220d W213", 2018, 199000),
    ("0951234567", "Наталья Кравец", "КА 8080 НК", "Mazda", "CX-5", 2019, 88300),
    ("0503456789", "Павел Гончар", "АІ 7777 ОК", "Opel", "Astra J", 2012, 245000),       # давно не был
    ("0674561234", "Светлана Руденко", "КА 1122 СВ", "Nissan", "Qashqai", 2017, 150300),  # должник
    ("0661239876", "Юлия Мельничук", "ВН 4455 КЕ", "Ford", "Focus III", 2014, 178000),   # «Готово к выдаче»
]

STOCK = [
    # название, артикул, бренд, закупка, кол-во, ячейка
    ("Фильтр масляный", "W 712/95", "MANN", "185", 10, "A / 1 / 1"),
    ("Фильтр воздушный", "C 30 005", "MANN", "320", 6, "A / 1 / 2"),
    ("Фильтр салона угольный", "CUK 26 017", "MANN", "410", 5, "A / 1 / 3"),
    ("Масло моторное 5W-30, 4 л", "Castrol EDGE 5W30", "Castrol", "1450", 8, "A / 2 / 1"),
    ("Колодки тормозные передние", "GDB1956", "TRW", "1120", 4, "B / 1 / 1"),
    ("Колодки тормозные задние", "GDB1858", "TRW", "890", 4, "B / 1 / 2"),
    ("Диск тормозной передний", "DF4296", "TRW", "1650", 2, "B / 2 / 1"),
    ("Свеча зажигания", "ILKAR7B11", "NGK", "390", 12, "A / 3 / 1"),
    ("Стойка стабилизатора передняя", "37301", "Lemförder", "640", 3, "B / 2 / 2"),
    ("Сайлентблок переднего рычага", "34567 01", "Lemförder", "520", 4, "B / 3 / 1"),
    ("Антифриз G12+, 5 л", "Coolstream G12+", "Coolstream", "780", 4, "A / 2 / 2"),
    ("Щётки стеклоочистителя (к-т)", "3 397 007 297", "Bosch", "690", 3, "A / 3 / 2"),
]


def _photo(db, wo, key, stage="intake", finding_id=None):
    full, thumb = PHOTOS / f"{key}.jpg", PHOTOS / f"{key}_t.jpg"
    if full.exists():
        osvc.save_photo(db, wo, full.read_bytes(), thumb.read_bytes() if thumb.exists() else None, stage, finding_id)


def populate():
    with SessionLocal() as db:
        from .models import Client
        if db.scalar(select(Client).limit(1)) is not None:
            return  # уже наполнено
        branch = db.scalar(select(Branch))
        current_branch_id.set(branch.id)
        users = {u.login: u for u in db.scalars(select(User))}

        def as_user(login):
            u = users[login]
            current_user_id.set(u.id)
            return SimpleNamespace(branch=branch, user=u, can=lambda *a: True)

        # --- Ещё слесари и посты ---
        as_user("admin")
        mech_role = db.scalar(select(Role).where(Role.code == "mechanic"))
        for login, name in EXTRA_MECHANICS:
            u = User(full_name=name, login=login, role_id=mech_role.id, password_hash=hash_secret(DEMO_PASSWORD))
            db.add(u)
            db.flush()
            users[login] = u
        # Второй менеджер и ещё запчастисты — пары «менеджер ↔ запчастист» (11.11.2026)
        for login, name, role_code in EXTRA_OFFICE:
            r_ = db.scalar(select(Role).where(Role.code == role_code))
            u = User(full_name=name, login=login, role_id=r_.id, password_hash=hash_secret(DEMO_PASSWORD))
            db.add(u)
            db.flush()
            users[login] = u
        for p_login, m_login in PAIRS.items():
            users[p_login].works_with_id = users[m_login].id
        lifts = {l.number: l for l in db.scalars(select(Lift))}
        names = {1: "Пост 1 · ТО", 2: "Пост 2 · Подвеска", 3: "Пост 3 · Тормоза", 4: "Пост 4 · Двигатель",
                 5: "Пост 5 · Развал", 6: "Пост 6 · Шиномонтаж"}
        for n, login in {1: "andrey", 2: "sergey", 3: "viktor", 4: "taras", 5: "igor"}.items():
            lifts[n].mechanic_id = users[login].id
        for n, name in names.items():
            lifts[n].name = name

        # --- Склад: стеллажи, приход по закупке ---
        as_user("pavel")
        for rack, shelves, places in (("A", 3, 4), ("B", 3, 4)):
            for s in range(1, shelves + 1):
                for p in range(1, places + 1):
                    db.add(Location(rack=rack, shelf=str(s), place=str(p), label=f"{rack} / {s} / {p}"))
        db.flush()
        locs = {l.label: l for l in db.scalars(select(Location))}
        from .models import PurchaseLine, Supplier
        as_user("oleg")
        sup = Supplier(name="Автотехнікс (демо)", phone="+380 44 000 00 00")
        sup2 = Supplier(name="Элит-Украина (демо)")
        db.add_all([sup, sup2])
        db.flush()
        stock_purchase = Purchase(supplier_id=sup.id, status="ordered", supplier_invoice="АТ-001245")
        db.add(stock_purchase)
        db.flush()
        cats = {}
        for name, art, brand, cost, qty, cell in STOCK:
            c = ssvc.get_or_create_catalog(db, name, art, brand)
            cats[name] = c
            db.add(PurchaseLine(purchase_id=stock_purchase.id, catalog_id=c.id, qty=Decimal(qty), price=Decimal(cost)))
        db.flush()
        as_user("pavel")
        for line in db.scalars(select(PurchaseLine).where(PurchaseLine.purchase_id == stock_purchase.id)).all():
            name = next(n for n, c in cats.items() if c.id == line.catalog_id)
            cell = next(s[5] for s in STOCK if s[0] == name)
            ssvc.receive_line(db, branch.id, line, locs[cell].id)
        ssvc.refresh_purchase_status(db, stock_purchase)
        # Закупки (11.11.2026): заказ у второго поставщика едет — пришёл частями; черновик списком из Excel
        as_user("vasyl")   # 12.11.2026: у каждого запчастиста свои закупки — эта Василия
        from datetime import date as _date, timedelta as _td
        p2 = Purchase(supplier_id=sup2.id, status="ordered", supplier_invoice="ЭУ-78040", ordered_on=_date.today() - _td(days=2),
                      expected_on=_date.today() + _td(days=1), note="Доставка до 16:00")
        db.add(p2)
        db.flush()
        p2_lines = []
        for r in ssvc.parse_paste("Амортизатор передний\t334842\tKYB\t4\t1850\nОпора амортизатора\tSM5226\tSasic\t2\t620\n"
                                  "Пыльник с отбойником\t910191\tKYB\t2\t340"):
            c = ssvc.get_or_create_catalog(db, r["name"], r["article"], r["brand"])
            ln = PurchaseLine(purchase_id=p2.id, catalog_id=c.id, qty=r["qty"], price=r["price"], received_qty=0)
            db.add(ln)
            p2_lines.append(ln)
        db.flush()
        as_user("pavel")
        ssvc.receive_line(db, branch.id, p2_lines[0], locs["A / 2 / 1"].id, Decimal(2))   # привезли 2 из 4
        ssvc.refresh_purchase_status(db, p2)
        as_user("denys")   # черновик Дениса
        draft = Purchase(supplier_id=sup.id, status="draft", note="Заказ на следующую неделю")
        db.add(draft)
        db.flush()
        for r in ssvc.parse_paste("Свеча зажигания; BKR6EK; NGK; 8; 180\nЩётка стеклоочистителя 600 мм; 3397008538; Bosch; 4; 420"):
            c = ssvc.get_or_create_catalog(db, r["name"], r["article"], r["brand"])
            db.add(PurchaseLine(purchase_id=draft.id, catalog_id=c.id, qty=r["qty"], price=r["price"], received_qty=0))
        db.flush()

        # Склад (10.11.2026): подкатегории и «подходит для» (марка, модель, годы) — для фильтра по машине
        from .models import PartFit
        demo_fit = {
            "Фильтр масляный": ("Фильтры", "Масляные", [("Volkswagen", "Passat B8", 2015, 2023), ("Skoda", "Octavia A7", 2013, 2020)]),
            "Фильтр воздушный": ("Фильтры", "Воздушные", [("Volkswagen", "Passat B8", 2015, 2023)]),
            "Фильтр салона угольный": ("Фильтры", "Салонные", [("Skoda", "Octavia A7", 2013, 2020), ("Volkswagen", "Golf VII", 2012, 2020)]),
            "Масло моторное 5W-30, 4 л": ("Масла", "Моторные", []),
            "Колодки тормозные передние": ("Тормоза", "Колодки", [("Renault", "Megane IV", 2016, None), ("Renault", "Kadjar", 2015, 2022)]),
            "Колодки тормозные задние": ("Тормоза", "Колодки", [("Mazda", "CX-5", 2017, None)]),
            "Диск тормозной передний": ("Тормоза", "Диски", [("Renault", "Megane IV", 2016, None)]),
            "Свеча зажигания": ("Свечи", "Зажигания", [("Toyota", "Camry", 2011, 2018), ("Toyota", "RAV4", 2013, 2018)]),
            "Стойка стабилизатора передняя": ("Ходовая и подвеска", "Стойки стабилизатора", [("Volkswagen", "Passat B8", 2015, 2023)]),
            "Сайлентблок переднего рычага": ("Ходовая и подвеска", "Рычаги и сайлентблоки", [("Hyundai", "Tucson", 2015, 2021), ("Kia", "Sportage", 2016, 2021)]),
            "Антифриз G12+, 5 л": ("Жидкости", "Антифриз", []),
        }
        for name, (cat_, sub_, fits_) in demo_fit.items():
            c = cats.get(name)
            if not c:
                continue
            c.category, c.subcategory = cat_, sub_
            for mk, md, y1, y2 in fits_:
                db.add(PartFit(catalog_id=c.id, make=mk, model=md, year_from=y1, year_to=y2))
        db.flush()

        # --- Наряды на разных этапах ---
        def intake(i, scenario, lift_n=None, partsman=True, works="", complaint="", fuel="50", items=("docs",),
                   note="", photos=("front", "rear", "left")):
            phone, name, plate, make, model, year, km = CLIENTS[i]
            ctx = as_user("irina")
            f = osvc.IntakeForm(phone=phone, name=name, plate=plate, make=make, model=model, year=str(year),
                                mileage=str(km), scenario=scenario, complaint=complaint, works=works,
                                partsman_id=str(users["oleg"].id) if partsman else "",
                                lift_id=str(lifts[lift_n].id) if lift_n else "", fuel_level=fuel,
                                intake_items=list(items), intake_note=note)
            wo = osvc.create_order(db, ctx, f)
            for key in photos:
                _photo(db, wo, key)
            osvc.route_after_intake(db, wo)
            db.flush()
            return wo

        def find(wo, items):
            as_user(db.get(User, wo.mechanic_id).login)
            out = []
            for text, urg, photo in items:
                fnd = osvc.add_finding(db, wo, text, urg)
                if photo:
                    _photo(db, wo, photo, "diag", fnd.id)
                osvc.event(db, wo, "finding", text, finding=fnd, urgency=urg)
                out.append(fnd)
            osvc.finish_diagnosis(db, wo, len(items))
            db.flush()
            return out

        def approve(wo, findings, answers):
            as_user("irina")
            for fnd, a in zip(findings, answers):
                fnd.approval = a
            db.flush()
            osvc.finish_approval(db, wo)
            db.flush()

        def add_part(wo, name, qty=1, markup=40, order=False, new=None):
            as_user("oleg")
            if new:  # новой детали нет на складе — заказ
                c = ssvc.get_or_create_catalog(db, name, new[0], new[1])
                cost = Decimal(new[2])
            else:
                c = cats[name]
                cost = Decimal(next(s[3] for s in STOCK if s[0] == name))
            part = WoPart(work_order_id=wo.id, catalog_id=c.id, name=c.name, qty=Decimal(qty), cost=cost,
                          markup_pct=markup, price=ssvc.sale_price(cost, markup), picked_by=users["oleg"].id)
            db.add(part)
            db.flush()
            if order or not ssvc.reserve_from_stock(db, branch.id, part):
                ssvc.order_part(db, branch.id, part)
            db.flush()
            return part

        # 1) Только приняли, слесарь не назначен
        intake(7, "B", None, complaint="Горит Check Engine, плавают обороты на холостом", fuel="25",
               items=("docs",), photos=("front", "odo"))

        # 2) На диагностике (пост 2)
        wo = intake(1, "B", 2, complaint="Стук спереди справа на кочках, течёт масло", fuel="75",
                    items=("docs",), photos=("front", "right", "odo"))
        as_user("sergey")
        f = osvc.add_finding(db, wo, "Стойка стабилизатора передняя правая — люфт, стучит", "urgent")
        _photo(db, wo, "link", "diag", f.id)
        osvc.event(db, wo, "finding", f.text, finding=f, urgency="urgent")

        # 3) Ждёт согласования с клиентом (пост 3)
        wo = intake(2, "B", 3, complaint="Скрипят тормоза, тянет вправо", fuel="50",
                    items=("docs",), note="Скол на лобовом стекле справа внизу",
                    photos=("front", "left", "scratch"))
        find(wo, [("Передние колодки 2 мм, диски с бортиком — менять комплектом", "urgent", "pads"),
                  ("Пыльник наружного ШРУСа левый порван", "soon", "boot"),
                  ("Задние шины: износ протектора ~70 %", "advice", "tyre")])
        # Менеджер сразу предлагает клиенту колодки трёх фирм — клиент выберет одну (11.11.2026)
        as_user("irina")
        grp = uuid.uuid4().hex
        for brand_, art_, price_ in (("TRW", "GDB1956", "1570"), ("Bosch", "0 986 494 104", "1290"),
                                     ("Оригинал", "3C0698151A", "3100")):
            c_ = ssvc.get_or_create_catalog(db, "Колодки тормозные передние", art_, brand_)
            db.add(WoPart(work_order_id=wo.id, catalog_id=c_.id, name=c_.name, qty=Decimal(1), price=Decimal(price_),
                          markup_pct=0, status="option", option_group=grp))
        db.flush()

        # 4) Подбор запчастей (сценарий А, пост 1 — ТО)
        intake(0, "A", 1, works="ТО-120: замена масла и фильтров\nЗамена свечей зажигания", fuel="50",
               items=("docs",), photos=("front", "rear"))

        # 5) Ждём запчасти от поставщика (пост 4)
        wo = intake(4, "B", 4, complaint="Течь масла под двигателем", fuel="100",
                    items=("docs", "valuables"), note="Клиент оставил в салоне детское кресло",
                    photos=("front", "right", "salon"))
        fs = find(wo, [("Течь масла с прокладки клапанной крышки", "soon", "oil")])
        approve(wo, fs, ["yes"])
        gasket = add_part(wo, "Прокладка клапанной крышки", new=("11127582245", "Elring", "1380"), markup=45)
        gasket.urgent = True   # слесарь просит срочно — видно в «Приходе», закупке и выдаче (11.11.2026)
        add_part(wo, "Масло моторное 5W-30, 4 л", markup=30)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        p = ssvc.draft_purchase(db, branch.id, None, wo.id)   # закупка Олега под этот наряд
        p.supplier_id, p.status, p.supplier_invoice = sup2.id, "ordered", "ЭУ-78111"
        p.ordered_on, p.expected_on = _date.today() - _td(days=3), _date.today() - _td(days=1)
        as_user("pavel")   # 12.11.2026: привезли — кладовщик ещё не принял («Привезли — ждут приёмки»)
        ssvc.mark_delivered(db, p, users["pavel"])

        # 6) Запчасти на складе, слесарь просит выдать → у кладовщика окошко (пост 5)
        wo = intake(3, "A", 5, works="Замена передних тормозных колодок\nЗамена передних тормозных дисков",
                    fuel="25", items=("docs",), photos=("front", "left"))
        add_part(wo, "Колодки тормозные передние", markup=40)
        add_part(wo, "Диск тормозной передний", qty=2, markup=35)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        as_user("igor")
        ssvc.request_issue(db, wo, "Машина уже на подъёмнике")

        # 6б) 12.11.2026: менеджер попросил выдать, кладовщик собрал у окошка — «Готово к выдаче», у менеджера горит
        wo = intake(11, "A", None, works="Замена масла и масляного фильтра", fuel="50", items=("docs",), photos=("front",))
        add_part(wo, "Фильтр масляный", markup=40)
        add_part(wo, "Масло моторное 5W-30, 4 л", markup=30)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        as_user("irina")
        req = ssvc.request_issue(db, wo, "Клиент ждёт, заберу сам")
        as_user("pavel")
        ssvc.mark_ready(db, req, users["pavel"])

        # 7) В работе: детали выданы (пост 6 — без закреплённого слесаря, ставим Андрея)
        wo = intake(5, "B", 6, complaint="Плановое ТО + стук в подвеске", fuel="75",
                    items=("docs",), photos=("front", "rear", "odo"))
        as_user("irina")
        wo.mechanic_id = users["andrey"].id
        osvc.set_status(db, wo, "diag", "Назначен слесарь")
        fs = find(wo, [("Сайлентблок переднего рычага левый — трещины", "soon", None),
                       ("Масло по регламенту — заменить", "soon", None),
                       ("Щётки стеклоочистителя оставляют полосы", "advice", None)])
        approve(wo, fs, ["yes", "yes", "no"])
        add_part(wo, "Сайлентблок переднего рычага", qty=2, markup=40)
        add_part(wo, "Масло моторное 5W-30, 4 л", markup=30)
        add_part(wo, "Фильтр масляный", markup=50)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        as_user("andrey")
        req = ssvc.request_issue(db, wo)
        as_user("pavel")
        ssvc.issue(db, req, users["pavel"].id)

        # 8) Готово: клиент отказался от ремонта
        wo = intake(6, "B", None, complaint="Проверить ходовую перед поездкой", fuel="50", items=("docs",),
                    photos=("front",))
        as_user("irina")
        wo.mechanic_id = users["sergey"].id
        osvc.set_status(db, wo, "diag", "Назначен слесарь")
        fs = find(wo, [("Ходовая в порядке. Задние колодки ~5 мм — заменить при следующем ТО", "advice", None)])
        approve(wo, fs, ["no"])

        # ---- Деньги и работы (шаг 6) ----
        from datetime import timedelta
        from .db import now
        from .models import PayScheme, PaySchemeLine, UserPayScheme, WoWork, WoWorkSession
        from datetime import date

        def price_works(wo, specs):
            """specs: [(часы или None, цена часа или фикс)] по порядку работ."""
            ws = db.scalars(select(WoWork).where(WoWork.work_order_id == wo.id).order_by(WoWork.created_at)).all()
            for w, (hours, rate) in zip(ws, specs):
                w.pricing = "fixed" if hours is None else "hours"
                w.hours = None if hours is None else Decimal(str(hours))
                w.rate = Decimal(str(rate))
                w.price = msvc.work_price(w)
            db.flush()
            return ws

        all_orders = db.scalars(select(WorkOrder).order_by(WorkOrder.created_at)).all()
        by_plate = {}
        from .models import Vehicle
        for o in all_orders:
            by_plate[db.get(Vehicle, o.vehicle_id).plate] = o
        P = osvc.norm_plate
        price_works(by_plate[P("АА 1234 ВС")], [(1.5, 800), (0.8, 800)])          # ТО Camry
        price_works(by_plate[P("АА 0001 ХХ")], [(2.5, 900)])                       # BMW течь масла
        price_works(by_plate[P("ВІ 3344 ОО")], [(1.2, 800), (None, 600)])          # Renault тормоза
        hy = by_plate[P("ВС 7777 АА")]                                             # Hyundai — в работе
        ws = price_works(hy, [(1.6, 800), (None, 450)])
        t0 = now() - timedelta(minutes=95)
        as_user("andrey")
        db.add(WoWorkSession(work_order_id=hy.id, wo_work_id=ws[0].id, mechanic_id=users["andrey"].id, lift_id=hy.lift_id,
                             started_at=t0, ended_at=t0 + timedelta(minutes=50)))
        ws[0].status = "done"; ws[0].done_at = t0 + timedelta(minutes=50)
        db.add(WoWorkSession(work_order_id=hy.id, wo_work_id=ws[1].id, mechanic_id=users["andrey"].id, lift_id=hy.lift_id,
                             started_at=t0 + timedelta(minutes=55), ended_at=t0 + timedelta(minutes=70)))
        db.add(WoWorkSession(work_order_id=hy.id, wo_work_id=ws[1].id, mechanic_id=users["andrey"].id, lift_id=hy.lift_id,
                             started_at=t0 + timedelta(minutes=70), kind="pause", pause_reason="Ждём масло от кладовщика"))
        ws[1].status = "paused"
        # BMW — большой ремонт с предоплатой (внесена)
        bmw = by_plate[P("АА 0001 ХХ")]
        as_user("irina")
        bmw.payment_type, bmw.prepay_amount, bmw.prepay_for = "prepay", Decimal("2000"), "Прокладка и масло"
        msvc.add_payment(db, bmw, Decimal("2000"), "card", "prepay", "000117")

        # Mazda: полностью сделан, оплачен со скидкой, выдан с рекомендацией
        done_wo = intake(8, "A", None, works="Замена тормозных колодок задних\nЗамена щёток стеклоочистителя",
                         fuel="75", items=("docs",), photos=("rear",))
        as_user("irina")
        done_wo.mechanic_id = users["viktor"].id
        ws = price_works(done_wo, [(0.8, 800), (None, 150)])
        for w in ws:
            w.mechanic_id = users["viktor"].id
        add_part(done_wo, "Колодки тормозные задние", markup=45)
        add_part(done_wo, "Щётки стеклоочистителя (к-т)", markup=40)
        as_user("oleg")
        ssvc.finish_picking(db, done_wo)
        as_user("viktor")
        req = ssvc.request_issue(db, done_wo)
        as_user("pavel")
        ssvc.issue(db, req, users["pavel"].id)
        as_user("viktor")
        for w in ws:
            w.status, w.done_at = "done", now()
        osvc.set_status(db, done_wo, "ready", "Все работы выполнены")
        as_user("irina")
        done_wo.discount_pct = Decimal("5")
        db.flush()
        due = msvc.totals(db, done_wo).due
        msvc.add_payment(db, done_wo, due, "terminal", "full", "000118")
        msvc.close(db, done_wo, False, "advice", "", "Передние диски — износ ~60 %, проверить через 10 000 км")

        # Слесарь Игорь: оклад 600 ₴/день + 25 % от работ (пример схемы)
        as_user("admin")
        sch = PayScheme(name="Схема: Игорь Мельник")
        db.add(sch); db.flush()
        db.add(UserPayScheme(user_id=users["igor"].id, scheme_id=sch.id, valid_from=date.today()))
        db.add(PaySchemeLine(scheme_id=sch.id, kind="salary", salary_period="day", rate=Decimal("600")))
        db.add(PaySchemeLine(scheme_id=sch.id, kind="percent", base="works", rate=Decimal("25")))

        # ---- Инструмент (шаг 6а) ----
        from .models import ToolCheckLine
        from .services import tools as tsvc
        kit = [("Набор головок 1/2\" 94 пр.", "4200", "94 предмета: головки 4–32 мм, трещотка, удлинители"),
               ("Ключ динамометрический 40–210 Нм", "3100", ""), ("Набор рожково-накидных ключей 8–24", "2650", "12 ключей"),
               ("Гайковёрт пневматический 1/2\"", "5400", ""), ("Набор отвёрток 12 пр.", "780", ""),
               ("Съёмник шаровых опор", "850", "")]
        as_user("pavel")
        n = 0
        for login in ("andrey", "sergey", "viktor", "taras", "igor", "roman"):
            for name, cost, desc in kit[: 6 if login in ("andrey", "taras") else 4]:
                n += 1
                t = Tool(inv_no=f"И-{n:04d}", name=name, serial=f"SN{2400 + n * 7}", cost=Decimal(cost), description=desc,
                         category="personal")
                db.add(t)
                db.flush()
                tsvc.move(db, t, users[login], "Личный инструмент: выдан при приёме на работу")
        # Общий инструмент — на складе, выдаётся под наряд (решение владельца 09.11.2026)
        shared = {}
        for name, cost, cat in (("Тестер аккумуляторов", "2300", "electric"), ("Компрессометр бензиновый", "1250", "engine"),
                                ("Эндоскоп автомобильный", "1900", "electric"), ("Ключ для масляных фильтров (краб)", "450", "to"),
                                ("Стяжки пружин (пара)", "1600", "chassis"), ("Съёмник ступиц универсальный", "3200", "pullers"),
                                ("Сепаратор тормозных цилиндров", "700", "brakes"), ("Гайковёрт аккумуляторный 1/2\"", "6800", "pneumo"),
                                ("Мультиметр", "950", "measure"), ("Фиксатор распредвалов (набор)", "2100", "engine")):
            n += 1
            t = Tool(inv_no=f"И-{n:04d}", name=name, serial=f"SN{2400 + n * 7}", cost=Decimal(cost), category=cat,
                     place=f"Шкаф {1 + n % 2} · полка {1 + n % 4}")   # общий инструмент — в шкафу на складе
            db.add(t)
            shared[name] = t
        db.flush()
        # Андрей взял стяжки и съёмник под работы по Hyundai; Виктор не сдал мультиметр после закрытого наряда
        hy_works = db.scalars(select(WoWork).where(WoWork.work_order_id == hy.id).order_by(WoWork.created_at)).all()
        tsvc.move(db, shared["Стяжки пружин (пара)"], users["andrey"], f"Под наряд {hy.number}", wo=hy, work=hy_works[0])
        tsvc.move(db, shared["Съёмник ступиц универсальный"], users["andrey"], f"Под наряд {hy.number}", wo=hy)
        tsvc.move(db, shared["Мультиметр"], users["viktor"], f"Под наряд {done_wo.number}", wo=done_wo)
        db.flush()
        # Запросы слесарей (11.11.2026): Игорь просит тестер под Renault, Андрей сдаёт съёмник — окошки у кладовщика
        as_user("igor")
        tsvc.request_issue(db, shared["Тестер аккумуляторов"], users["igor"], by_plate[P("ВІ 3344 ОО")])
        as_user("andrey")
        tsvc.request_return(db, shared["Съёмник ступиц универсальный"], users["andrey"])
        db.flush()
        # Виктор: проверка при приёме — всё исправно
        c1 = tsvc.open_check(db, users["viktor"], "hire")
        db.flush()
        for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c1.id)):
            l.result = "ok"
        tsvc.finish_check(db, c1)
        # Тарас: плановая проверка — ключ сломан по вине (удержано), отвёртки — естественный износ
        c2 = tsvc.open_check(db, users["taras"], "planned")
        db.flush()
        for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c2.id)):
            tn = db.get(Tool, l.tool_id).name
            if tn.startswith("Ключ динамометрический"):
                l.result, l.charge, l.comment = "fault", Decimal("1500"), "Использовал как рычаг — погнут корпус. Удержание: стоимость ремонта"
            elif tn.startswith("Набор отвёрток"):
                l.result, l.comment = "worn", "Стёрты жала — естественный износ"
            else:
                l.result = "ok"
        tsvc.finish_check(db, c2)
        as_user("admin")
        tsvc.approve_check(db, c2, users["admin"], {str(l.id) for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c2.id))})
        # Андрей: плановая проверка начата кладовщиком (можно открыть и провести самому)
        as_user("pavel")
        tsvc.open_check(db, users["andrey"], "planned")
        # Запасные наборы (11.11.2026) — лежат в шкафу, выдаются взамен сломанного личного
        for name, cost, desc in (("Набор головок 1/2\" 94 пр. (запасной)", "4200", "Полный комплект, в кейсе"),
                                 ("Набор рожково-накидных ключей 8–24 (запасной)", "2650", "12 ключей")):
            n += 1
            db.add(Tool(inv_no=f"И-{n:04d}", name=name, serial=f"SN{2400 + n * 7}", cost=Decimal(cost), description=desc,
                        category="spare", place="Шкаф 1 · полка 4"))
        db.flush()
        tsvc.adopt_text_places(db, branch.id)   # «Шкаф 1 · полка 2» → шкафы и полки справочника

        # ---- Разбор (шаг 7): доноры, б/у детали, продажи ----
        from .models import Donor, DonorPhoto, StockPhoto
        from .services import razbor as rsvc
        as_user("dmitro")
        demo_donors = [
            (dict(make="BMW", model="X5 E70", year=2009, vin="WBAFE41000LX12345", engine="3.0 дизель M57", gearbox="АКПП 6HP",
                  body="кроссовер", color="чёрный", mileage=312000, purchase_price=Decimal("45000"), expenses=Decimal("6500"),
                  expenses_note="Эвакуатор 2500, разборка 4000", notes="Удар в заднюю часть, перед целый"),
             ("front", "left", "salon"),
             [("Фара передняя левая, ксенон", "6500", "good", "B / 3 / 2", "link", "Без трещин, крепления целые"),
              ("Зеркало правое с поворотником", "2200", "excellent", "B / 3 / 3", "", ""),
              ("Блок управления двигателем DDE", "8900", "good", "A / 3 / 3", "", "Проверен на стенде"),
              ("Руль кожаный с подушкой", "4800", "fair", "B / 3 / 4", "salon", "Потёртости на коже"),
              ("Генератор 180A", "3900", "good", "A / 3 / 4", "", ""),
              ("Подрамник передний", "3500", "repair", "", "", "Нужна покраска")]),
            (dict(make="Toyota", model="Camry XV40", year=2008, vin="JTNBE40K403123456", engine="2.4 бензин 2AZ", gearbox="АКПП",
                  body="седан", color="серебристый", mileage=268000, purchase_price=Decimal("38000"), expenses=Decimal("3000"),
                  expenses_note="Разборка", status="done"),
             ("rear", "right"),
             [("Дверь передняя правая в сборе", "5200", "good", "B / 2 / 4", "right", ""),
              ("Коробка передач АКПП U250E", "14500", "good", "", "", "Пробег 268 тыс., переключается мягко"),
              ("Капот", "3800", "fair", "", "front", "Скол краски"),
              ("Фонарь задний левый", "1600", "excellent", "B / 2 / 3", "rear", "")]),
        ]
        sold_plan = {"Коробка передач АКПП U250E": ("СТО «Мотор» (Киев)", "0442223344", "np", "20450987654321", "card"),
                     "Капот": ("Сергей", "0501112233", "pickup", "", "cash")}
        reserve_plan = {"Фара передняя левая, ксенон": ("Олег М.", "0633332211", "np"),
                        "Генератор 180A": ("Василий", "0991112200", "pickup"),
                        # 12.11.2026: «Выдача и отправка» — посылка готова к отправке, дверь едет личным курьером
                        "Фонарь задний левый": ("Ирина В.", "0507771122", "np"),
                        "Дверь передняя правая в сборе": ("СТО «Кузов» (Бровары)", "0445556677", "other")}
        # упаковано кладовщиком разбора: (где лежит, ТТН)
        packed_plan = {"Генератор 180A": ("у окошка", ""), "Фонарь задний левый": ("полка отправок 1", "20450555000111")}
        # 11.11.2026: посылка в пути — наложенный платёж, ждём деньги
        sent_plan = {"Блок управления двигателем DDE": ("Андрей К.", "0671234500", "Львов, отд. 7", "20450123456789")}
        for info, d_photos, parts in demo_donors:
            d = Donor(code=rsvc.next_donor_code(db, branch.id), bought_on=date.today() - timedelta(days=20), **info)
            db.add(d)
            db.flush()
            for k in d_photos:
                f = PHOTOS / f"{k}.jpg"
                if f.exists():
                    db.add(DonorPhoto(donor_id=d.id, file=rsvc.save_photo(f.read_bytes())))
            for name, price, cond, cell, photo, desc in parts:
                item = rsvc.add_part(db, branch.id, d, name, Decimal(1), Decimal(price), cond,
                                     locs[cell].id if cell else None, desc, publish=True)
                if photo and (PHOTOS / f"{photo}.jpg").exists():
                    db.add(StockPhoto(stock_item_id=item.id, file=rsvc.save_photo((PHOTOS / f"{photo}.jpg").read_bytes())))
                db.flush()
                if name in sold_plan:
                    bn, bp, dl, ttn, pm = sold_plan[name]
                    rsvc.sell_now(db, branch.id, item, 1, Decimal(price) - 300, bn, bp, dl, pm, ttn)
                elif name in reserve_plan:
                    bn, bp, dl = reserve_plan[name]
                    s = rsvc.reserve_for_buyer(db, branch.id, item, 1, Decimal(price), bn, bp, dl,
                                               np_address={"np": "Киев, отд. 112", "other": "Бровары, ул. Киевская 10"}.get(dl, ""),
                                               channel="olx" if dl == "np" else "phone")
                    db.flush()
                    if dl == "other":
                        s.carrier_id = next(c.id for c in rsvc.carriers(db, branch.id) if c.kind == "courier")
                    if name in packed_plan:
                        rsvc.pack(db, s, users["dmitro"], *packed_plan[name][:1], None, packed_plan[name][1])
                elif name in sent_plan:
                    bn, bp, addr, ttn = sent_plan[name]
                    s = rsvc.reserve_for_buyer(db, branch.id, item, 1, Decimal(price) - 300, bn, bp, "np", "card",
                                               np_address=addr, cod=True, channel="site")
                    db.flush()
                    rsvc.ship(db, s, ttn, cod=True)

        # ---- Табель: отметки по картам за прошлый и текущий месяц ----
        import random
        from datetime import datetime as _dt, time as _t, timezone as _tz
        from .models import AccessPoint, AttendanceEvent
        from .services.attendance import local_tz
        rnd = random.Random(7)
        gate = db.scalar(select(AccessPoint).where(AccessPoint.kind == "gate"))
        today = date.today()
        start_day = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        tz = local_tz()

        def mark(u, d, hh, mm, direction, method="card", comment=""):
            at = _dt.combine(d, _t(hh, mm), tzinfo=tz).astimezone(_tz.utc)
            db.add(AttendanceEvent(user_id=u.id, point_id=gate.id if gate else None, direction=direction, at=at,
                                   method=method, comment=comment))

        workers = [u for login, u in users.items() if login != "admin"]
        fired_on = today - timedelta(days=FIRED_DAYS_AGO)
        d = start_day
        while d < today:
            for u in workers:
                if u.login == "roman" and d > fired_on:
                    continue                        # уволен — отметок больше нет
                wd = d.weekday()
                if wd == 6 or (wd == 5 and u.role.code not in ("mechanic", "manager")):
                    continue                        # воскресенье — выходной, в субботу работают слесари и менеджер
                if rnd.random() < 0.06:
                    continue                        # иногда — отгул/больничный
                mark(u, d, 7 + (rnd.random() < 0.7), rnd.randint(0, 59) if rnd.random() < 0.3 else rnd.randint(40, 59), "in")
                if d == today - timedelta(days=1) and u.login == "sergey":
                    continue                        # вчера забыл отметить уход — в табеле будет «?»
                end_h = 14 if wd == 5 else 17 + (rnd.random() < 0.5)
                mark(u, d, end_h, rnd.randint(0, 59), "out")
            d += timedelta(days=1)
        # Сегодня: кто-то уже на месте, кто-то ещё нет, один пришёл и ушёл (отпросился)
        for login, hh, mm in (("irina", 7, 55), ("andrey", 7, 48), ("oleg", 8, 5), ("pavel", 7, 58), ("viktor", 8, 12), ("taras", 8, 20)):
            mark(users[login], today, hh, mm, "in")
        mark(users["taras"], today, 11, 30, "out")
        # Ручная правка админом (видно в журнале)
        as_user("admin")
        mark(users["dmitro"], today - timedelta(days=2) if (today - timedelta(days=2)).weekday() < 5 else today - timedelta(days=4),
             9, 0, "in", "manual", "Забыл карту дома")

        # Заметки в карты клиентов
        as_user("irina")
        for i, text in ((0, "Постоянный клиент, просит звонить после 18:00"),
                        (4, "Ездит далеко — предупреждать о сроках заранее")):
            from .models import Client
            c = db.scalar(select(Client).where(Client.phone == osvc.norm_phone(CLIENTS[i][0])))
            osvc.event(db, None, "note", text, client_id=c.id)

        # ---- Запись клиентов на неделю вперёд (и пара прошедших: приехал / не приехал) ----
        as_user("irina")
        from .models import Appointment
        from .services.schedule import to_utc
        plan = [  # через сколько дней, время, минут, мастер, имя, телефон, авто, номер, что делать, статус
            (0, "15:00", 60, "sergey", "Оксана Литвин", "0671230001", "Kia Sportage", "АА 5521 КС", "Замена масла и фильтров", "booked"),
            (0, "17:00", 90, "andrey", "Роман Гнатюк", "0501230002", "Ford Focus", "ВС 7310 АН", "Стук в подвеске спереди", "booked"),
            (1, "09:00", 120, "andrey", "Игорь Мельник", "0631230003", "VW Passat B7", "КА 3301 ВО", "ТО-90 000, проверить тормоза", "booked"),
            (1, "11:30", 60, "viktor", "Светлана Бойко", "0971230004", "Toyota RAV4", "АІ 4410 ЕК", "Замена тормозных колодок", "booked"),
            (1, "14:00", 180, "taras", "Максим Кравец", "0681230005", "BMW 320d", "ВІ 9902 АМ", "Течь масла, диагностика двигателя", "booked"),
            (2, "10:00", 60, "sergey", "Анна Шевчук", "0731230006", "Hyundai Tucson", "АА 0717 ТТ", "Развал-схождение", "booked"),
            (2, "12:00", 240, "andrey", "Денис Олейник", "0661230007", "Skoda Superb", "КА 2244 МР", "Замена ремня ГРМ с помпой", "booked"),
            (3, "09:30", 90, "igor", "Юрий Ковальчук", "0951230008", "Renault Duster", "СВ 1180 АР", "Шумит подшипник ступицы", "booked"),
            (4, "16:00", 60, "viktor", "Мария Ткаченко", "0681230009", "Mazda 6", "АА 6080 ММ", "Диагностика кондиционера", "booked"),
            (-1, "10:00", 60, "andrey", "Олег Савчук", "0501230010", "Opel Astra", "ВН 3434 АК", "Замена свечей", "noshow"),
        ]
        for days, hhmm, dur, mech, name, phone, car, plate, work, status in plan:
            db.add(Appointment(start_at=to_utc(today + timedelta(days=days), hhmm), duration_min=dur,
                               mechanic_id=users[mech].id if mech in users else None, client_name=name,
                               phone=osvc.norm_phone(phone), car=car, plate=osvc.norm_plate(plate), work=work, status=status))

        # ---- Выплаты зарплаты (аванс) — чтобы в «Зарплате» было видно «выплачено / к выплате» ----
        as_user("admin")
        from .models import PayrollPayout
        for login, amount in (("andrey", 3000), ("sergey", 2500), ("irina", 4000)):
            if login in users:
                db.add(PayrollPayout(user_id=users[login].id, period_from=today.replace(day=1), period_to=today,
                                     amount=Decimal(amount), paid_on=today.replace(day=min(5, today.day)), method="cash",
                                     note="Аванс"))
        # Недельная зарплата (11.11.2026): часть начислений — прошлая неделя, её уже проверил бухгалтер (ждёт админа)
        from .models import PayrollAccrual
        from .services import payweek as wsvc
        db.flush()
        last_mon = wsvc.monday(today) - timedelta(days=7)
        for k, a in enumerate(db.scalars(select(PayrollAccrual).where(PayrollAccrual.wo_work_id.is_not(None))
                                         .order_by(PayrollAccrual.created_at)).all()):
            if k % 2 == 0:
                a.on_date = last_mon + timedelta(days=k % 6)
        if "buh" in users:
            w = wsvc.get_week(db, branch.id, last_mon, create=True)
            wsvc.set_status(db, w, "checked", users["buh"])
            w.note = f"{today:%d.%m} Оксана: сверила с нарядами, всё сходится"
        users["andrey"].pay_method, users["andrey"].pay_account, users["andrey"].pay_bank = "card", "4149000000001234", "ПриватБанк"
        users["sergey"].pay_method = "cash"

        # ---- Уволенный слесарь Роман: сделал наряд, получил расчёт, сдал инструмент → «Архив» ----
        from .services import staff as stsvc
        roman = users["roman"]
        roman.hired_on = today - timedelta(days=240)
        old_wo = intake(0, "A", None, partsman=False, works="Замена масла двигателя\nЗамена салонного фильтра",
                        fuel="50", items=("docs",), photos=("front",))
        as_user("irina")
        old_wo.mechanic_id = roman.id
        ws = price_works(old_wo, [(0.6, 800), (None, 250)])
        as_user("roman")
        for w in ws:
            w.mechanic_id, w.status, w.done_at = roman.id, "done", now() - timedelta(days=FIRED_DAYS_AGO + 3)
        osvc.set_status(db, old_wo, "ready", "Все работы выполнены")
        as_user("irina")
        db.flush()
        msvc.add_payment(db, old_wo, msvc.totals(db, old_wo).due, "cash", "full", "")
        msvc.close(db, old_wo, False, "ok", "", "")
        as_user("admin")
        from .models import PayrollAccrual
        db.add(PayrollAccrual(user_id=roman.id, base_amount=Decimal(1), rate=Decimal(6000), amount=Decimal(6000),
                              on_date=fired_on, kind="salary", note="Оклад за отработанные дни"))
        db.add(PayrollPayout(user_id=roman.id, period_from=fired_on.replace(day=1), period_to=fired_on,
                             amount=Decimal(5000), paid_on=fired_on, method="cash", note="Расчёт при увольнении (часть)"))
        as_user("pavel")
        c3 = tsvc.open_check(db, roman, "dismissal")
        db.flush()
        tsvc.finish_check(db, c3)
        as_user("admin")
        stsvc.dismiss(db, roman, users["admin"], fired_on, "По собственному желанию — переезд в другой город", {})

        # ---- Клиенты (10.11.2026): постоянные, давно не были, должник ----
        from .models import Client as _Client
        for i in (0, 4):  # отмечены «постоянными» вручную
            db.scalar(select(_Client).where(_Client.phone == osvc.norm_phone(CLIENTS[i][0]))).is_regular = True
        lost = intake(9, "A", None, partsman=False, works="Замена масла и фильтра", fuel="50", items=("docs",), photos=("front",))
        as_user("irina")
        price_works(lost, [(0.5, 700)])
        lost.status, lost.closed_at = "closed", now() - timedelta(days=220)
        lost.created_at = now() - timedelta(days=221)
        debtor = intake(10, "A", None, partsman=False, works="Замена передних колодок", fuel="50", items=("docs",),
                        photos=("front",))
        as_user("irina")
        dw = price_works(debtor, [(1.0, 800)])
        dw[0].mechanic_id, dw[0].status, dw[0].done_at = users["sergey"].id, "done", now() - timedelta(days=3)
        debtor.mechanic_id = users["sergey"].id
        osvc.set_status(db, debtor, "ready", "Все работы выполнены")
        db.flush()
        msvc.add_payment(db, debtor, Decimal("300"), "cash", "extra", "")
        as_user("admin")
        msvc.close(db, debtor, True, "ok", "", "", "Остаток доплатит в пятницу — договорились по телефону")

        current_user_id.set(None)
        db.commit()
