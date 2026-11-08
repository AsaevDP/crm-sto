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
from .models import (Branch, Lift, Location, Purchase, Role, User, WoFinding, WoPart, WorkOrder)
from .security import hash_secret
from .services import orders as osvc
from .services import money as msvc
from .services import stock as ssvc

PHOTOS = Path(__file__).parent / "demo" / "photos"

EXTRA_MECHANICS = [("viktor", "Виктор Слюсаренко"), ("taras", "Тарас Гайко"), ("igor", "Игорь Мельник")]

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
               items=("docs", "keys2"), photos=("front", "odo"))

        # 2) На диагностике (пост 2)
        wo = intake(1, "B", 2, complaint="Стук спереди справа на кочках, течёт масло", fuel="75",
                    items=("docs", "spare", "jack"), photos=("front", "right", "odo"))
        as_user("sergey")
        f = osvc.add_finding(db, wo, "Стойка стабилизатора передняя правая — люфт, стучит", "urgent")
        _photo(db, wo, "link", "diag", f.id)
        osvc.event(db, wo, "finding", f.text, finding=f, urgency="urgent")

        # 3) Ждёт согласования с клиентом (пост 3)
        wo = intake(2, "B", 3, complaint="Скрипят тормоза, тянет вправо", fuel="50",
                    items=("docs", "spare", "kit"), note="Скол на лобовом стекле справа внизу",
                    photos=("front", "left", "scratch"))
        find(wo, [("Передние колодки 2 мм, диски с бортиком — менять комплектом", "urgent", "pads"),
                  ("Пыльник наружного ШРУСа левый порван", "soon", "boot"),
                  ("Задние шины: износ протектора ~70 %", "advice", "tyre")])

        # 4) Подбор запчастей (сценарий А, пост 1 — ТО)
        intake(0, "A", 1, works="ТО-120: замена масла и фильтров\nЗамена свечей зажигания", fuel="50",
               items=("docs", "spare"), photos=("front", "rear"))

        # 5) Ждём запчасти от поставщика (пост 4)
        wo = intake(4, "B", 4, complaint="Течь масла под двигателем", fuel="100",
                    items=("docs", "keys2", "valuables"), note="Клиент оставил в салоне детское кресло",
                    photos=("front", "right", "salon"))
        fs = find(wo, [("Течь масла с прокладки клапанной крышки", "soon", "oil")])
        approve(wo, fs, ["yes"])
        add_part(wo, "Прокладка клапанной крышки", new=("11127582245", "Elring", "1380"), markup=45)
        add_part(wo, "Масло моторное 5W-30, 4 л", markup=30)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        p = ssvc.draft_purchase(db, branch.id)
        p.supplier_id, p.status, p.supplier_invoice = sup2.id, "ordered", "ЭУ-77812"

        # 6) Запчасти на складе, слесарь просит выдать → у кладовщика окошко (пост 5)
        wo = intake(3, "A", 5, works="Замена передних тормозных колодок\nЗамена передних тормозных дисков",
                    fuel="25", items=("docs",), photos=("front", "left"))
        add_part(wo, "Колодки тормозные передние", markup=40)
        add_part(wo, "Диск тормозной передний", qty=2, markup=35)
        as_user("oleg")
        ssvc.finish_picking(db, wo)
        as_user("igor")
        ssvc.request_issue(db, wo, "Машина уже на подъёмнике")

        # 7) В работе: детали выданы (пост 6 — без закреплённого слесаря, ставим Андрея)
        wo = intake(5, "B", 6, complaint="Плановое ТО + стук в подвеске", fuel="75",
                    items=("docs", "spare"), photos=("front", "rear", "odo"))
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
                         fuel="75", items=("docs", "spare"), photos=("rear",))
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

        # Заметки в карты клиентов
        as_user("irina")
        for i, text in ((0, "Постоянный клиент, просит звонить после 18:00"),
                        (4, "Ездит далеко — предупреждать о сроках заранее")):
            from .models import Client
            c = db.scalar(select(Client).where(Client.phone == osvc.norm_phone(CLIENTS[i][0])))
            osvc.event(db, None, "note", text, client_id=c.id)

        current_user_id.set(None)
        db.commit()
