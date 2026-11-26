"""«Живая» проверка CRM (12.11.2026): неделя работы СТО — только кнопками, как люди.
20.11.2026 (брат): новая имитация — 20–30 машин, насыщенные наряды, новый каталог; на сегодня — машина под каждую кнопку.

Каждое действие — та же форма, что отправляет экран, под тем сотрудником, кто это делает в жизни
(те же права, проверки, сообщения). В базу напрямую НИЧЕГО не пишется. Скрипт только:
  • читает базу, чтобы «увидеть экран» (номера нарядов, id позиций — как человек видит их глазами);
  • переносит время: всё, что сделано в группе действий, сдвигается на нужный день и час в прошлом.

Не сработало (ошибка сервера, отказ, красное сообщение) или после действия получилось не то — строка в отчёте.
Сценарии — docs/simulation.md.

Запуск (рабочую базу не трогает — своя папка data/sim):
    .venv\\Scripts\\python scripts\\simulate.py            → data/sim/crm.db + data/sim/report.md
    .venv\\Scripts\\python scripts\\simulate.py --install  → поставить data/sim/crm.db рабочей базой
                                                          (старая — в data/backup-before-sim-*.db; CRM остановить)
"""
import os
import random
import re
import shutil
import sqlite3
import sys
import time as _time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "data" / "sim"
DAYS = int(os.getenv("SIM_DAYS", "27"))   # 26.11.2026 (брат): четыре недели истории (пн–пт) — большая история зарплаты (для пробы: SIM_DAYS=3)
SEED = 1211

sys.path.insert(0, str(ROOT))


def install():
    src, dst = SIM / "crm.db", ROOT / "data" / "crm.db"
    if not src.exists():
        sys.exit("Сначала запустите прогон: scripts/simulate.py")
    if dst.exists():
        b = ROOT / "data" / f"backup-before-sim-{datetime.now():%Y%m%d-%H%M}.db"
        s, d = sqlite3.connect(dst), sqlite3.connect(b)
        s.backup(d)
        d.close(), s.close()
        print(f"Прежняя база сохранена: {b.name}")
    # Копия средствами SQLite: работает, даже если базу держит открытой другой сервер (журнал -wal он ведёт сам)
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    d.close(), s.close()
    # фото нарядов, разбора, инструмента — тоже в рабочую папку (имена случайные, ничего не затираем)
    shutil.copytree(SIM / "photos", ROOT / "data" / "photos", dirs_exist_ok=True)
    print("Готово: рабочая база = результат живого прогона (вместе с фото)")


if "--install" in sys.argv:
    install()
    sys.exit()

# ---------------------------------------------------------------- отдельная база для прогона
if SIM.exists():
    shutil.rmtree(SIM)
SIM.mkdir(parents=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(SIM / 'crm.db').as_posix()}"
os.environ["CRM_DATA_DIR"] = str(SIM)
os.environ.pop("DEMO", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Date, func, select, update  # noqa: E402

from app.db import Base, SessionLocal, UTCDateTime  # noqa: E402
from app.main import app  # noqa: E402
from app import models as M  # noqa: E402

# 23.11.2026 (брат): покрытие — какие кнопки (POST-адреса) и экраны (GET) прогон реально нажал / открыл.
# В конце отчёта — список того, что ни разу не нажималось.
HIT = Counter()
# служебное — не кнопки и не экраны для человека (файлы, отладка, вход/выход сайта)
SKIP_ROUTES = {"/static", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/favicon.ico"}


@app.middleware("http")
async def _cover(request, call_next):
    resp = await call_next(request)
    route = request.scope.get("route")
    if route is not None and resp.status_code < 500:
        HIT[(request.method, getattr(route, "path", ""))] += 1
    return resp

rnd = random.Random(SEED)
TODAY = date.today()
START = TODAY - timedelta(days=DAYS)
PHOTOS = sorted((ROOT / "app" / "demo" / "photos").glob("*.jpg"))
LOCAL = datetime.now().astimezone().tzinfo


# ================================================================ отчёт
class Report:
    def __init__(self):
        self.fails = []        # (день, кто, что, адрес, проблема)
        self.logic = []        # проверки логики после действий
        self.ok = Counter()
        self.day = START

    def fail(self, who, what, path, problem):
        self.fails.append((self.day, who, what, path, problem))

    def check(self, cond, what, detail=""):
        if not cond:
            self.logic.append((self.day, what, detail))
        return cond


R = Report()


# ================================================================ время
KEEP_TIME = {("appointments", "start_at")}


def _shift(mark: datetime, target: datetime):
    """Всё, что создано или изменено после mark, — на время target (сохраняя порядок действий)."""
    delta = mark - target
    if abs(delta.total_seconds()) < 1:
        return
    real_today = date.today()
    with SessionLocal() as db:
        for t in Base.metadata.sorted_tables:
            if "id" not in t.c:
                continue
            # 20.11.2026: время записи клиента человек вписал сам («завтра 09:00») — его не двигаем (раньше записи
            # на завтра оказывались на 02:45, 04:07…)
            dts = [c for c in t.c if isinstance(c.type, UTCDateTime) and (t.name, c.name) not in KEEP_TIME]
            ds = [c for c in t.c if isinstance(c.type, Date) and not isinstance(c.type, UTCDateTime)]
            key = t.c.get("updated_at") if "updated_at" in t.c else (t.c.get("created_at") if "created_at" in t.c else None)
            if key is None or not dts:
                continue
            for r in db.execute(select(t.c.id, *dts, *ds).where(key >= mark)).all():
                m = r._mapping
                vals = {c.name: m[c.name] - delta for c in dts if m[c.name] is not None and m[c.name] >= mark}
                vals.update({c.name: target.date() for c in ds if m[c.name] == real_today})
                if vals:
                    db.execute(update(t).where(t.c.id == r.id).values(**vals))
        db.commit()


TMAX = [""]


@contextmanager
def at(day: date, hhmm: str):
    """Группа действий «в этот день в это время»."""
    R.day = day
    TMAX[0] = max(TMAX[0], hhmm)   # самое позднее время шага — следующий шаг наряда не раньше (история по порядку)
    mark = datetime.now(timezone.utc)
    _time.sleep(0.002)
    yield
    hh, mm = (int(x) for x in hhmm.split(":"))
    _shift(mark, datetime.combine(day, time(hh, mm), tzinfo=LOCAL).astimezone(timezone.utc))


# ================================================================ «люди» — у каждого свой браузер
FLASH_ERR = re.compile(r'<div class="flash error" role="status">(.*?)</div>', re.S)   # общее сообщение страницы
PASS = {}


class People:
    def __init__(self):
        self.c = {}

    def __call__(self, login) -> TestClient:
        if login not in self.c:
            c = TestClient(app)
            r = c.post("/login", data={"login": login, "password": PASS.get(login, login + "123")}, follow_redirects=False)
            if r.status_code != 303:
                R.fail(login, "вход", "/login", f"не пустило ({r.status_code})")
            self.c[login] = c
        return self.c[login]


P = People()


def post(who, path, data=None, files=None, what="", fetch=False, ok_json=True, expect_no=False):
    """expect_no — 23.11.2026: кнопка, которая здесь ДОЛЖНА отказать (красное сообщение — это правильно)."""
    if expect_no:
        r = P(who).post(path, data=data or {}, files=files, follow_redirects=True)
        if r.status_code >= 500:
            R.fail(who, what or path, path, f"ответ {r.status_code}")
        elif not FLASH_ERR.search(r.text):
            R.fail(who, what or path, path, "ждали отказ (красное сообщение) — а прошло")
        else:
            R.ok[who] += 1
        return r
    hdr = {"x-requested-with": "fetch"} if fetch else {}
    try:
        r = P(who).post(path, data=data or {}, files=files, headers=hdr, follow_redirects=not fetch)
    except Exception as e:   # ошибка сервера внутри
        R.fail(who, what or path, path, f"ошибка сервера: {type(e).__name__}: {e}"[:300])
        return None
    if r.status_code >= 400:
        txt = r.text[:200].replace("\n", " ")
        R.fail(who, what or path, path, f"ответ {r.status_code}: {txt}")
        return r
    if fetch or r.headers.get("content-type", "").startswith("application/json"):
        try:
            j = r.json()
            if ok_json and isinstance(j, dict) and j.get("ok") is False:
                R.fail(who, what or path, path, "отказ: " + "; ".join(j.get("errors") or [str(j.get("error", ""))]))
        except ValueError:
            pass
        return r
    m = FLASH_ERR.search(r.text)
    if m and "Записано, но есть пересечение" in m.group(1):   # 23.11.2026: предупреждение — запись сохранена
        m = None
    if m:
        R.fail(who, what or path, path, "красное сообщение: " + re.sub(r"<[^>]+>", "", m.group(1)).strip())
    else:
        R.ok[who] += 1
    return r


def get(who, path, what=""):
    try:
        r = P(who).get(path)
    except Exception as e:
        R.fail(who, what or "открыть " + path, path, f"ошибка сервера: {type(e).__name__}: {e}"[:300])
        return None
    if r.status_code >= 400:
        R.fail(who, what or "открыть " + path, path, f"ответ {r.status_code}")
    return r


def photo(n=1):
    out = []
    for i in range(n):
        f = rnd.choice(PHOTOS)
        out.append(("photos", (f"p{i}.jpg", f.read_bytes(), "image/jpeg")))
    return out


# ================================================================ «глаза» — что человек видит на экране
def q(model, *where, order=None):
    with SessionLocal() as db:
        s = select(model).where(*where)
        if order is not None:
            s = s.order_by(order)
        return db.scalars(s).all()


def one(model, *where):
    with SessionLocal() as db:
        return db.scalar(select(model).where(*where).limit(1))


def uid(login):
    return one(M.User, M.User.login == login).id


def free_qty(catalog_id) -> Decimal:
    from app.services import stock as ssvc
    with SessionLocal() as db:
        items = db.scalars(select(M.StockItem).where(M.StockItem.catalog_id == catalog_id)).all()
        bal = ssvc.balances(db, items[0].branch_id, [i.id for i in items]) if items else {}
        return sum((b["free"] for b in bal.values()), Decimal(0))


def cat_id(name):
    c = one(M.PartCatalog, M.PartCatalog.name == name)
    return c.id if c else None


# ================================================================ справочники прогона
# 12.11.2026 (брат): состав СТО для ручной проверки — по одному человеку на должность, кладовщиков двое
# (склад и разбор). Логины прежние (пароль = логин + 123, PIN как в docs/test-users.md), имена — настоящие.
NAMES = {
    "admin": "Администратор", "irina": "Ирина Коваленко", "oleg": "Олег Савчук", "pavel": "Павел Гончаренко",
    "dmitro": "Дмитрий Лысенко", "andrey": "Андрей Демченко", "director": "Виктор Петренко", "deputy": "Наталья Кравец",
    "buh": "Оксана Мельничук", "elektro": "Роман Бойко", "motor": "Василий Ткачук", "moyka": "Артём Литвиненко",
    "detail": "Ольга Захарченко",
    "glav": "Сергей Ковальчук",   # 20.11.2026: главный кладовщик — обе зоны склада
}
# 23.11.2026 (брат): второй менеджер, второй слесарь, второй запчастист — без них не проверить передачу наряда,
# пары «менеджер ↔ запчастист», «менеджер видит только свои наряды», второго мастера на машине
STAFF_EXTRA = [("marina", "Марина Ткаченко", "manager"), ("taras", "Тарас Мороз", "mechanic"),
               ("yuriy", "Юрий Кравчук", "partsman")]
NAMES.update({lg: nm for lg, nm, _ in STAFF_EXTRA})
PAIRS = {"oleg": "irina", "yuriy": "marina"}
MANAGERS = ["irina", "irina", "marina"]   # у Ирины больше машин, у Марины — свои
PARTSMEN = {"irina": ["oleg"], "marina": ["yuriy", "oleg"]}   # 23.11.2026: у Марины свой запчастист
MECHS = ["andrey", "andrey", "taras"]   # 23.11.2026: второй слесарь — Тарас

# 20.11.2026 (брат): новая имитация — новый каталог (другие детали, фирмы, артикулы), наряды насыщеннее:
# у каждой работы по нескольку деталей со склада и под заказ, работ в наряде 2–4, второй мастер чаще.
OIL = "Масло моторное 5W-30 C3, 5 л"            # на эти четыре позиции ссылаются сценарии «на сегодня»
PADS = "Колодки тормозные передние (к-т)"
CLEANER = "Очиститель тормозов и деталей, 600 мл"
LAMP = "Лампа H4 галогенная"
STOCK = [  # название; артикул; бренд; кол-во; цена закупки — первая закупка «на склад»
    (OIL, "Magnatec C3 5W30", "Castrol", 24, 1780), ("Масло моторное 0W-20, 4 л", "Mobil 1 ESP X2", "Mobil", 12, 1950),
    ("Масло трансмиссионное 75W-90, 1 л", "Gear Oil GL-5", "Motul", 10, 420),
    ("Фильтр масляный", "HU 7020 z", "MANN", 24, 230), ("Фильтр воздушный", "LX 2620", "Mahle", 16, 360),
    ("Фильтр салона", "CUK 2939", "MANN", 14, 380), ("Фильтр топливный", "KL 156/3", "Mahle", 6, 720),
    ("Прокладка сливной пробки", "N 0138157", "VAG", 40, 25),
    ("Свеча зажигания иридиевая", "FR7KI332S", "Bosch", 32, 460), ("Катушка зажигания", "0 221 604 115", "Bosch", 4, 1650),
    (PADS, "0 986 494 525", "Bosch", 10, 1240), ("Колодки тормозные задние (к-т)", "P 85 075", "Brembo", 8, 1080),
    ("Диск тормозной передний", "09.A428.11", "Brembo", 8, 1720), ("Диск тормозной задний", "BG4318", "Delphi", 6, 1190),
    ("Датчик износа колодок", "WK 128", "Textar", 8, 240),
    ("Стойка стабилизатора передняя", "FE41018", "Febi", 10, 590), ("Втулка стабилизатора", "SB-0129", "CTR", 12, 140),
    ("Шаровая опора", "JBJ793", "TRW", 6, 760), ("Сайлентблок переднего рычага", "BH21-0098", "Moog", 8, 480),
    ("Наконечник рулевой тяги", "JTE7563", "TRW", 6, 610), ("Тяга рулевая", "JAR1102", "TRW", 4, 940),
    ("Антифриз G13, 5 л", "Protect G13", "Febi", 8, 940), ("Тормозная жидкость DOT 4 LV, 1 л", "LV DOT4", "ATE", 10, 330),
    ("Щётки стеклоочистителя (к-т)", "AeroTwin A979S", "Bosch", 8, 760), (LAMP, "64193", "Osram", 18, 150),
    ("Лампа W5W (2 шт)", "2825-02B", "Osram", 20, 60),
    ("Ремень поликлиновой", "6PK1153", "Contitech", 6, 560), ("Ролик натяжной", "VKM 36034", "SKF", 4, 980),
    ("Аккумулятор 70 Ач", "570 500 065", "Bosch", 3, 3900), ("Датчик ABS передний", "SS20048", "Delphi", 3, 1080),
    ("Смазка литиевая, 400 г", "Litol-24", "Liqui Moly", 6, 190), (CLEANER, "BC600", "Wynn's", 14, 175),
]
CHEM = [  # химия для мойки — закупка из «Мойки» (запчастист)
    ("Пена активная для бесконтактной мойки, 20 л", "", "Grass", 4, 2600), ("Автошампунь с воском, 1 л", "", "Koch Chemie", 8, 310),
    ("Воск быстрый (спрей), 0,5 л", "", "Meguiar's", 5, 640), ("Полироль пластика матовая, 0,5 л", "", "Sonax", 6, 380),
    ("Очиститель кожи, 0,5 л", "", "Leather Master", 4, 590), ("Гель для чернения шин, 0,5 л", "", "Sonax", 6, 290),
    ("Микрофибра для сушки 60×90", "", "", 20, 240), ("Антибитум, 0,5 л", "", "Koch Chemie", 4, 330),
    ("Очиститель стёкол, 0,5 л", "", "Sonax", 8, 160), ("Полировальная паста абразивная, 250 г", "", "3M", 3, 980),
]
# Работы по специальностям: (вес, название, часы или None, ставка/фикс, [(запчасть со склада, кол-во)],
#                            заказать_новую [(название, артикул, бренд, цена)])
JOBS = {
    "mechanic": [
        (10, "Плановое ТО (масло, фильтры)", 1.0, 800, [(OIL, 1), ("Фильтр масляный", 1), ("Фильтр воздушный", 1),
                                                        ("Фильтр салона", 1), ("Прокладка сливной пробки", 1)], []),
        (6, "Замена передних колодок и датчика износа", 0.9, 800, [(PADS, 1), ("Датчик износа колодок", 1)], []),
        (4, "Замена передних дисков и колодок", 1.6, 800, [(PADS, 1), ("Диск тормозной передний", 2), (CLEANER, 1)], []),
        (4, "Замена задних дисков и колодок", 1.5, 800, [("Колодки тормозные задние (к-т)", 1), ("Диск тормозной задний", 2)], []),
        (4, "Замена свечей и катушки зажигания", 0.9, 800, [("Свеча зажигания иридиевая", 4), ("Катушка зажигания", 1)], []),
        (4, "Ремонт передней подвески", 2.5, 850, [("Стойка стабилизатора передняя", 2), ("Втулка стабилизатора", 2),
                                                   ("Шаровая опора", 1)], [("Рычаг передний нижний", "FE30451", "Febi", 2350)]),
        (3, "Замена сайлентблоков рычагов", 2.0, 850, [("Сайлентблок переднего рычага", 2), ("Смазка литиевая, 400 г", 1)], []),
        (3, "Развал-схождение", None, 900, [], []),
        (3, "Диагностика ходовой", None, 400, [], []),
        (2, "Замена ремня ГРМ с помпой", 3.5, 900, [("Антифриз G13, 5 л", 1)], [("Комплект ГРМ с помпой", "CT1028WP2", "Contitech", 4650)]),
        (2, "Замена приводного ремня и ролика", 1.0, 800, [("Ремень поликлиновой", 1), ("Ролик натяжной", 1)], []),
        (2, "Замена передних амортизаторов", 2.0, 850, [], [("Амортизатор передний", "22-266767", "Bilstein", 2600),
                                                             ("Опора амортизатора", "802 389", "Sachs", 690)]),
        (2, "Замена рулевых тяг и наконечников", 1.4, 800, [("Тяга рулевая", 2), ("Наконечник рулевой тяги", 2)], []),
        (2, "Замена тормозной жидкости", 0.8, 800, [("Тормозная жидкость DOT 4 LV, 1 л", 1)], []),
        (2, "Замена масла в коробке", 0.8, 800, [("Масло трансмиссионное 75W-90, 1 л", 2)], []),
    ],
    "electrician": [
        (5, "Компьютерная диагностика", None, 600, [], []),
        (3, "Замена генератора", 1.5, 900, [("Ремень поликлиновой", 1)], [("Генератор 140 А", "0 986 080 750", "Bosch", 7400)]),
        (2, "Замена стартера", 1.2, 900, [], [("Стартер", "LRS02417", "Lucas", 4900)]),
        (3, "Замена датчика ABS", 0.7, 850, [("Датчик ABS передний", 1)], []),
        (3, "Замена аккумулятора", None, 250, [("Аккумулятор 70 Ач", 1)], []),
        (2, "Замена ламп и щёток", None, 300, [(LAMP, 2), ("Лампа W5W (2 шт)", 1), ("Щётки стеклоочистителя (к-т)", 1)], []),
        (2, "Ремонт проводки", 2.0, 850, [], []),
    ],
    "motorist": [
        (3, "Замена прокладки ГБЦ", 6.0, 950, [("Антифриз G13, 5 л", 1)], [("Прокладка ГБЦ", "61-36310-10", "Victor Reinz", 1850),
                                                                         ("Болты ГБЦ (к-т)", "14-32252-01", "Victor Reinz", 980)]),
        (2, "Замена маслосъёмных колпачков", 5.0, 950, [(OIL, 1)], [("Колпачки маслосъёмные (к-т)", "12-31306-01", "Victor Reinz", 640)]),
        (1, "Капремонт двигателя", 18.0, 950, [(OIL, 1), ("Фильтр масляный", 1)],
         [("Поршневая группа (к-т)", "87-427200-00", "Kolbenschmidt", 14800), ("Вкладыши коренные (к-т)", "77554600", "Kolbenschmidt", 2100)]),
    ],
    "washer": [(6, "Мойка кузова", None, 350, [], []), (4, "Комплексная мойка", None, 650, [], [])],
    "detailer": [(3, "Полировка кузова", None, 4500, [], []), (3, "Химчистка салона", None, 3500, [], []),
                 (4, "Полировка фар", None, 800, [], [])],
}
MASTERS_OF = {"mechanic": MECHS, "electrician": ["elektro"], "motorist": ["motor"], "washer": ["moyka"], "detailer": ["detail"]}
COMPLAINTS = {"mechanic": ["Стучит спереди на кочках", "Скрипят тормоза", "Пора на ТО", "Тянет вправо", "Вибрация руля на скорости"],
              "electrician": ["Не заводится по утрам", "Горит лампа ABS", "Садится аккумулятор", "Не работает фара"],
              "motorist": ["Течёт антифриз, белый дым", "Ест масло, дымит", "Двигатель троит, потеря мощности"]}

FIRST = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Алексей", "Иван", "Михаил", "Юрий", "Олег", "Анна", "Мария",
         "Елена", "Ольга", "Наталья", "Ирина", "Светлана", "Виктория", "Тарас", "Богдан", "Роман", "Людмила", "Галина", "Павел"]
LAST = ["Шевченко", "Бондаренко", "Ткаченко", "Кравченко", "Олейник", "Коваль", "Марченко", "Лысенко", "Руденко", "Савчук",
        "Мельник", "Петренко", "Клименко", "Павленко", "Левченко", "Гончаренко", "Сидоренко", "Ярошенко", "Онищенко", "Захарченко"]
CARS = [("Toyota", "Camry"), ("Toyota", "RAV4"), ("Volkswagen", "Passat B8"), ("Volkswagen", "Golf VII"), ("Skoda", "Octavia A7"),
        ("Skoda", "Superb"), ("Renault", "Megane IV"), ("Renault", "Duster"), ("Hyundai", "Tucson"), ("Kia", "Sportage"),
        ("Ford", "Focus III"), ("Mazda", "CX-5"), ("Nissan", "Qashqai"), ("BMW", "X5 F15"), ("Mercedes-Benz", "E 220d W213"),
        ("Audi", "A6 C7"), ("Mitsubishi", "Outlander"), ("Honda", "CR-V"), ("Lexus", "RX 350"), ("Opel", "Astra J")]
REG, LET = ["АА", "КА", "АІ", "ВІ", "ВС", "СВ", "ВН", "АХ"], "АВСЕНІКМОРТХ"


WMI = {"Toyota": "JTN", "Volkswagen": "WVW", "Skoda": "TMB", "Renault": "VF1", "Hyundai": "TMA", "Kia": "U5Y",
       "Ford": "WF0", "Mazda": "JMZ", "Nissan": "SJN", "BMW": "WBA", "Mercedes-Benz": "WDD", "Audi": "WAU",
       "Mitsubishi": "JMB", "Honda": "SHS", "Lexus": "JTJ", "Opel": "W0L"}
VIN_CH = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"   # в VIN нет I, O, Q
VIN_YEAR = "ABCDEFGHJKLMNPRSTVWXY123456789"    # код года: A=2010 … Y=2030, 1=2031… (2001–2009 — цифры)


def make_vin(make, year, r=None):
    """Реалистичный VIN (12.11.2026): код завода по марке, год, серийный номер."""
    r = r or rnd
    y = VIN_YEAR[(year - 2010) % 30] if year >= 2010 else str(year - 2000)
    return (WMI.get(make, "XTA") + "".join(r.choice(VIN_CH) for _ in range(6)) + y + r.choice(VIN_CH)
            + "".join(r.choice("0123456789") for _ in range(6)))


def make_clients(n):
    out, used = [], set()
    for i in range(n):
        while True:
            plate = f"{rnd.choice(REG)} {rnd.randint(1000, 9999)} {rnd.choice(LET)}{rnd.choice(LET)}"
            if plate not in used:
                used.add(plate)
                break
        make, model = rnd.choice(CARS)
        year = rnd.randint(2008, 2022)
        out.append({"phone": f"0{rnd.choice((50, 63, 66, 67, 68, 73, 93, 95, 96, 97, 98, 99))}{2000000 + i * 7919 % 999999:07d}",
                    "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", "plate": plate, "make": make, "model": model,
                    "year": year, "km": (TODAY.year - year) * rnd.randint(14000, 21000), "visits": 0,
                    "vin": make_vin(make, year)})
    return out


CLIENTS = make_clients(46)   # 23.11.2026: до 50 машин — все кнопки (сколько машин — проверяет final_checks)   # 22.11.2026 (брат): плотнее — 35–40 машин, 50–60 нарядов


def free_client(repeat=False):
    """Клиент, у которого сейчас нет открытого наряда (одна машина — один открытый наряд).
    repeat — чаще постоянные: у них есть история визитов (карточка клиента, «Прошлый визит», гарантия)."""
    busy = {o.c["plate"] for o in ACTIVE if o.wo() is not None and o.wo().status not in ("closed", "cancelled")}
    pool = [c for c in CLIENTS if c["plate"] not in busy] or CLIENTS
    if repeat and rnd.random() < 0.25:   # 22.11.2026: чаще новые машины (35–40 машин за две недели)
        pool = [c for c in pool if c["visits"]] or pool
    return rnd.choice(pool)


# ================================================================ подготовка СТО (админ, первые дни)
def setup():
    d0 = START - timedelta(days=3)
    roles = {r.code: r.id for r in q(M.Role)}
    with at(d0, "09:00"):
        post("admin", "/settings/branding", {"name": "СТО «Мотор»", "accent": "blue", "idle_minutes": "30"}, what="Оформление")
        post("admin", "/settings/pricing", {"default_markup": "40", "hour_rate": "800"}, what="Цены: наценка, норма-час")
        for login, name, role in STAFF_EXTRA:
            post("admin", "/settings/users", {"full_name": name, "login": login, "role_id": str(roles[role]),
                                              "password": login + "123", "pin": ""}, what=f"Сотрудник {name}")
        # 12.11.2026 (брат): кладовщиков двое — Павел на складе, Дмитрий только на разборе (без прихода и выдачи склада)
        rc = one(M.Role, M.Role.code == "content")
        if rc:
            post("admin", f"/settings/roles/{rc.id}/color", {"color": rc.color, "name": "Кладовщик разбора"}, what="Роль: кладовщик разбора")
            for perm in ("stock.manage", "stock.issue"):
                post("admin", f"/settings/roles/{rc.id}/perm", {"perm": perm, "on": ""}, what=f"Роль разбора без {perm}", fetch=True)
        for p, m in PAIRS.items():
            post("admin", "/settings/pairs", {"partsman_id": str(uid(p)), "manager_id": str(uid(m))}, what="Пара менеджер ↔ запчастист")
        # 20.11.2026 (брат): «Зарплату сотрудников» директору открывает админ (галочка в правах сотрудника)
        post("admin", f"/settings/users/{uid('director')}/perm", {"perm": "payroll.view_all", "mode": "allow"},
             what="Директору: «Зарплата сотрудников»", fetch=True)
    with at(d0, "09:30"):
        # Карты для табеля — каждому
        for i, u in enumerate(q(M.User, M.User.login != "admin")):
            if not one(M.StaffCard, M.StaffCard.user_id == u.id):
                post("admin", f"/settings/users/{u.id}/cards", {"uid": f"77{i:08d}", "label": "пропуск"}, what=f"Карта {u.full_name}")
        # Рабочие места (21.11.2026): подъёмники 1–3, электрик 1, моторист 1, мойка 1, детейлинг 1; остальные выключены
        places = {("lift", 1): ("Слесарный", "andrey"), ("lift", 2): ("Подвеска", "taras"), ("lift", 3): ("Развал", None),
                  ("electric", 1): ("", "elektro"), ("motor", 1): ("", "motor"), ("wash", 1): ("", "moyka"),
                  ("detail", 1): ("", "detail")}
        for l in q(M.Lift, order=M.Lift.number):
            key = (l.kind, l.number)
            name, m = places.get(key, (l.name, None))
            on = key in places
            if on or l.active:
                post("admin", f"/settings/lifts/{l.id}", {"kind": l.kind, "number": str(l.number), "name": name,
                                                          "mechanic_id": str(uid(m)) if m else "", "active": "1" if on else ""},
                     what=f"Рабочее место {l.label}")
        # Склад: стеллажи A (ТО), B (ремонт), Х (химия)
        for rack, sh, pl in (("A", 4, 5), ("B", 4, 5), ("Х", 2, 4)):
            post("pavel", "/stock/locations", {"rack": rack, "shelves": str(sh), "places": str(pl)}, what=f"Стеллаж {rack}")
        # Шкаф инструмента и общий инструмент
        post("pavel", "/tools/cabinets", {"action": "add", "name": "Шкаф 1 · общий", "shelves": "3"}, what="Шкаф инструмента")
    shelves = q(M.ToolShelf)
    with at(d0, "10:00"):
        for name, cost, cat in (("Динамометрический ключ 40–200 Нм", 4200, "measure"), ("Съёмник шаровых опор", 1800, "pullers"),
                                ("Мультиметр Fluke", 6500, "electric"), ("Набор для замены ГРМ", 7800, "engine"),
                                ("Компрессометр", 2300, "engine"), ("Полировальная машинка", 9800, "body")):
            post("pavel", "/tools", {"name": name, "category": cat, "cost": str(cost),
                                     "shelf_id": str(shelves[0].id) if shelves else ""}, what=f"Инструмент {name}")
        for m, name, cost in (("andrey", "Набор головок 1/2\" 94 пр.", 3900), ("andrey", "Набор ключей комбинированных 8–22", 2100),
                              ("motor", "Набор головок 1/2\" 94 пр.", 3900), ("elektro", "Мультиметр UNI-T UT33", 900),
                              ("elektro", "Набор отвёрток диэлектрических", 1100)):
            post("pavel", "/tools", {"name": name, "category": "personal", "cost": str(cost),
                                     "holder_id": str(uid(m))}, what=f"Личный инструмент {m}")
        # Проходная: устройство на входе в цех
        pt = one(M.AccessPoint, M.AccessPoint.kind == "gate")
        if pt:
            post("admin", f"/settings/points/{pt.id}/kiosk", what="Проходная — киоск")
    # Зарплата: оклады офиса (схемы — по умолчанию из настроек)
    with at(d0, "10:30"):
        post("admin", "/settings/payroll/defaults", {"mechanic_works_pct": "40", "partsman_margin_pct": "20"}, what="Схемы по умолчанию")
        for login, amount in (("irina", 700), ("pavel", 600), ("dmitro", 550), ("buh", 650)):
            post("admin", f"/settings/payroll/{uid(login)}/lines", {"kind": "salary", "base": "fixed", "rate": str(amount),
                                                                     "salary_period": "day"}, what=f"Оклад {login}")
    with at(d0, "11:00"):   # прайс мойки заводится при первом открытии (18.11.2026)
        get("irina", "/wash/prices", what="Прайс мойки")
        s = one(M.WashService, M.WashService.name == "Мойка кузова ручная")
        if s:   # менеджер правит цену в прайсе (сохраняется сама)
            post("irina", f"/wash/prices/{s.id}", {"name": s.name, "kind": s.kind, "price_car": "380", "price_suv": "430",
                                                   "price_bus": "550", "minutes": "30"}, what="Прайс мойки: новая цена", fetch=True)
        for name in ("Мойка 1", "Детейлинг"):
            post("irina", "/wash/boxes", {"name": name}, what=f"Бокс мойки «{name}»")


def first_stock():
    d = START - timedelta(days=2)
    with at(d, "10:00"):
        r = post("oleg", "/purchases/new", {"supplier_new": "Автотехнікс"}, what="Закупка на склад")
        pid = r.url.path.rsplit("/", 1)[1] if r is not None else None
        # 22.11.2026: история — две недели, склад закупаем на две недели (вдвое больше)
        k = max(1, round(DAYS / 6.5))   # 26.11.2026: на всю историю (4 недели — вчетверо)
        paste = "\n".join(f"{n}; {a}; {b}; {qty * k}; {c}" for n, a, b, qty, c in STOCK)
        post("oleg", f"/purchases/{pid}/lines", {"paste": paste}, what="Строки списком (Excel)")
        post("oleg", f"/purchases/{pid}", {"action": "save", "expected_on": str(d + timedelta(days=1)),
                                           "supplier_invoice": "АТ-00871", "supplier_phone": "+380445550011"}, what="Дата «привезут»")
        post("oleg", f"/purchases/{pid}/ordered", {"back": "/my-orders"}, what="✓ Заказал")
    with at(d + timedelta(days=1), "11:00"):
        receive(pid, partial=False)
    with at(d, "11:00"):
        # 20.11.2026 (брат): химию для мойки заказывает запчастист («Мойка» → «Заказать химию»)
        r = post("oleg", "/wash/purchases/new", {"supplier_new": "Химтрейд"}, what="Закупка химии (запчастист)")
        cpid = r.url.path.rsplit("/", 1)[1] if r is not None else None
        paste = "\n".join(f"{n}; {a}; {b}; {qty}; {c}" for n, a, b, qty, c in CHEM)
        post("oleg", f"/purchases/{cpid}/lines", {"paste": paste}, what="Химия списком")
        post("oleg", f"/purchases/{cpid}/ordered", {}, what="✓ Заказал химию")
    with at(d + timedelta(days=1), "12:00"):
        receive(cpid, partial=False, rack="Х")
    # Проверка: химия ушла в раздел «Химия», запчасти — нет
    for n, *_ in CHEM:
        c = one(M.PartCatalog, M.PartCatalog.name == n)
        R.check(c is not None and c.section == "chem", "химия из закупки для мойки — в разделе «Химия»", n)
    c = one(M.PartCatalog, M.PartCatalog.name == CLEANER)
    R.check(c is not None and c.section != "chem", "очиститель тормозов — не химия для мойки", c.section if c else "нет")


def receive(pid, partial=False, rack="A"):
    """Кладовщик: «✓ Принять полностью» или «Неполная поставка» (вписал, сколько привезли)."""
    if not pid:
        return
    # 14.11.2026 (брат): шага «Привезли» нет — кладовщик сразу принимает и вписывает № накладной
    lines = q(M.PurchaseLine, M.PurchaseLine.purchase_id == uuid.UUID(str(pid)), M.PurchaseLine.received.is_(False))
    locs = q(M.Location, M.Location.rack == rack) or q(M.Location)
    data = {"all": pid, "invoice": f"ТТН-{rnd.randint(10000, 99999)}"}
    # 14.11.2026 (брат): неполная поставка бывает двух видов — одной детали меньше штук или одну деталь НЕ привезли
    # совсем (0). Раньше прогон делал только первый, и ошибка «0 → накладная пропадает» не ловилась.
    short = next((l for l in lines if Decimal(l.qty) - (l.received_qty or 0) >= 2), None)
    missing = lines[-1] if len(lines) >= 2 and (short is None or rnd.random() < 0.5) else None
    if missing is not None:
        short = None
    partial = partial and (short is not None or missing is not None)
    if partial:
        data["short"] = "1"   # форма «Неполная поставка → Принять привезённое»
    for i, l in enumerate(lines):
        if l.location_id is None:
            from app.services import stock as ssvc
            with SessionLocal() as db:
                dflt = ssvc.default_location(db, l.catalog_id)
            data[f"loc_{l.id}"] = str(dflt or locs[i % len(locs)].id)
        if partial:
            left = Decimal(l.qty) - (l.received_qty or 0)
            data[f"qty_{l.id}"] = str(0 if l is missing else (left - 1 if l is short else left))
    what = ("Неполная поставка (одну деталь не привезли)" if missing is not None else "Неполная поставка (меньше штук)") \
        if partial else "✓ Принять полностью"
    post("pavel", f"/purchases/{pid}/receive", data, what=what)
    if partial:   # накладная должна стать «неполная поставка» и быть видна и там, и в «Принятых»
        p = one(M.Purchase, M.Purchase.id == uuid.UUID(str(pid)))
        R.check(p.status == "partial", "неполная поставка → накладная «неполная поставка»", f"{pid}: {p.status}")
        # 17.11.2026: кладовщик видит это в «Приходе» («Неполная поставка», «Приняты» — по накладным);
        # в «Закупках» у него только свои — там это видит тот, кто заказывал, и директор
        r = get("pavel", "/stock/receive?tab=partial", what="Приход → Неполная поставка")
        R.check(r is not None and f"/purchases/{pid}" in r.text, "неполная поставка видна в «Приходе → Неполная поставка»", str(pid))
        r = get("pavel", "/stock/receive?tab=got", what="Приход → Приняты")
        R.check(r is not None and f"№ {data['invoice']}" in r.text, "принятое видно в «Приходе → Приняты» по № накладной", str(pid))
        r = get("director", f"/purchases?tab=arrived", what="Закупки → Приняты (директор)")
        R.check(r is not None and f"/purchases/{pid}" in r.text, "неполная поставка у директора в «Закупках → Приняты»", str(pid))


# ================================================================ наряд — путь машины по кнопкам
class Order:
    def __init__(self, client, mgr, kind, jobs, scen, lead, extra):
        self.c, self.mgr, self.kind, self.jobs, self.scen, self.lead, self.extra = client, mgr, kind, jobs, scen, lead, extra
        self.id = None
        self.step = "intake"
        self.wait_until = None
        self.notes = []
        self.fail_plan = None

    @property
    def url(self):
        return f"/orders/{self.id}"

    def wo(self):
        return one(M.WorkOrder, M.WorkOrder.id == self.id) if self.id else None


N_ORDERS = [0]
ACTIVE: list[Order] = []
DONE: list[Order] = []


def pick_job(kind):
    js = JOBS[kind]
    return rnd.choices(js, weights=[j[0] for j in js])[0]


def new_order(day, mgr, hhmm, from_appt=None, kind=None, scen=None):
    c = from_appt["client"] if from_appt else free_client(repeat=True)
    forced = kind is not None
    kind = kind or rnd.choices(["mechanic", "electrician", "motorist", "washer", "detailer"], weights=(62, 16, 8, 8, 6))[0]
    jobs = [pick_job(kind)]
    # 20.11.2026: наряды насыщеннее — у слесаря 2–3 разные работы, у электрика и моториста — ещё одна
    want = rnd.choice((2, 3, 3)) if kind == "mechanic" else 2 if kind in ("electrician", "motorist") else 1
    for _ in range(12):
        if len(jobs) >= want:
            break
        j = pick_job(kind)
        if j[1] not in {x[1] for x in jobs}:
            jobs.append(j)
    extra = []   # вторые мастера на той же машине
    if kind in ("mechanic", "motorist") and rnd.random() < 0.35:
        extra.append(("electrician", pick_job("electrician")))
    if kind == "mechanic" and rnd.random() < 0.15:
        extra.append(("motorist", pick_job("motorist")))
    if kind in ("mechanic", "electrician", "motorist") and rnd.random() < 0.3:
        extra.append(("washer", pick_job("washer")))
    if kind == "washer" and rnd.random() < 0.3:
        extra.append(("detailer", pick_job("detailer")))
    lead = rnd.choice(MASTERS_OF[kind])
    scen = scen or ("A" if kind in ("washer", "detailer") or rnd.random() < 0.55 else "B")
    o = Order(c, mgr, kind, jobs, scen, lead, extra)
    # Неудачные сценарии — по кругу, чтобы каждый точно случился несколько раз
    N_ORDERS[0] += 1
    k = N_ORDERS[0]
    # 20.11.2026: машин мало — каждый неудачный сценарий ровно по разу в первых нарядах недели (дальше — обычные)
    o.fail_plan = {2: "part_no", 3: "reopen", 4: "cancel", 5: "return_part", 7: "all_no", 9: "part_no"}.get(k)
    lift = one(M.Lift, M.Lift.mechanic_id == uid(lead))
    pm = rnd.choice(PARTSMEN[mgr])
    c["visits"] += 1
    c["km"] += rnd.randint(1500, 9000)
    data = {"phone": c["phone"], "name": c["name"], "plate": c["plate"], "make": c["make"], "model": c["model"],
            "year": str(c["year"]), "mileage": str(c["km"]), "scenario": scen, "vin": c["vin"],
            "complaint": rnd.choice(COMPLAINTS.get(kind, ["Помыть машину"])) if scen == "B" else "; ".join(j[1] for j in jobs),
            "works": "\n".join(j[1] for j in jobs) if scen == "A" else "",
            "partsman_id": str(uid(pm)), "mechanic_id": "" if (scen == "B" and rnd.random() < 0.3 and not forced) else str(uid(lead)), "lift_id": str(lift.id) if lift else "",
            "fuel_level": rnd.choice(("25", "50", "75")), "intake_items": ["docs"] + (["spare"] if rnd.random() < 0.5 else []),
            "intake_token": f"sim-{rnd.random()}"}
    if from_appt:
        data["appt_id"] = from_appt["id"]
    with at(day, hhmm):
        r = post(mgr, "/orders/new", data, files=photo(rnd.randint(1, 3)), what=f"Приёмка {c['plate']}", fetch=True)
    if r is None or r.status_code != 200:
        return None
    o.id = uuid.UUID(r.json()["url"].rsplit("/", 1)[1])
    o.last = (day, hhmm)
    o.born = day
    o.step = "diag" if scen == "B" else "price"
    ACTIVE.append(o)
    return o


def works_of(o):
    return q(M.WoWork, M.WoWork.work_order_id == o.id, order=M.WoWork.created_at)


def parts_of(o):
    return q(M.WoPart, M.WoPart.work_order_id == o.id, order=M.WoPart.created_at)


def step_diag(o, day, hhmm, approve=True):
    """Диагностика (или менеджер записал со слов клиента) → «Согласование». 13.11.2026: дальше одновременно —
    запчастист подбирает, менеджер составляет работы; согласование с клиентом — отдельным шагом (step_approve)."""
    wo = o.wo()
    if wo.status == "diag" or (o.kind in ("mechanic", "electrician", "motorist") and rnd.random() < 0.8):
        if wo.status == "created":   # мастер не назначен при приёмке — отправляет менеджер
            with at(day, hhmm):
                lift = one(M.Lift, M.Lift.mechanic_id == uid(o.lead))
                post(o.mgr, f"{o.url}/to-diag", {"mechanic_id": str(uid(o.lead)), "lift_id": str(lift.id) if lift else ""},
                     what="На диагностику")
        R.check(o.wo().status == "diag", "сценарий Б с мастером → сразу «Диагностика»", o.wo().status)
        with at(day, _later(hhmm, 50)):
            texts = [j[1] for j in o.jobs]
            if rnd.random() < 0.3:
                texts.append(rnd.choice(["Износ передних шин ~70 %", "Течь масла из-под клапанной крышки", "Трещина лобового стекла"]))
            for t in texts:
                post(o.lead, f"{o.url}/findings", {"text": t, "urgency": rnd.choice(("urgent", "soon", "soon", "advice"))},
                     files=photo(1) if rnd.random() < 0.7 else None, what="Находка с фото", fetch=True)
            post(o.lead, f"{o.url}/diag-done", what="Диагностика готова")
    else:   # менеджер записал со слов клиента
        with at(day, hhmm):
            for j in o.jobs:
                post(o.mgr, f"{o.url}/findings", {"text": j[1], "urgency": "soon"}, what="Проблема со слов клиента", fetch=True)
            post(o.mgr, f"{o.url}/to-approve", what="Сразу к согласованию")
    R.check(o.wo().status == "approve", "после диагностики наряд «Согласование»", f"{wo.number}: {o.wo().status}")
    o.step = "price"


def _later(hhmm, minutes):
    h, m = (int(x) for x in hhmm.split(":"))
    t = min(h * 60 + m + minutes, 19 * 60 + 30)
    return f"{t // 60:02d}:{t % 60:02d}"


def job_by_name(name):
    for kind, js in JOBS.items():
        for j in js:
            if j[1] == name:
                return kind, j
    return None, None


def step_price(o, day, hhmm):
    """23.11.2026 (брат): шаг «Работы» — менеджер по проблемам пишет работы (цена, время, исполнитель; вторые мастера —
    отдельной работой) и жмёт «Работы записаны → на подбор». Только после этого запчастист подбирает."""
    with at(day, hhmm):
        job_names = {j[1] for j in o.jobs}
        have = {w.name for w in works_of(o)}
        for f in q(M.WoFinding, M.WoFinding.work_order_id == o.id, order=M.WoFinding.created_at):
            if f.text in have:
                continue
            if f.text in job_names or rnd.random() < 0.35:   # лишняя находка — иногда тоже в работы (клиент решит)
                post(o.mgr, f"{o.url}/works", {"name": f.text, "pricing": "fixed", "rate": "500", "qty": "1"},
                     what="Работа по проблеме")
        for w in works_of(o):
            kind, j = job_by_name(w.name)
            if not j:
                continue
            master = o.lead if kind == o.kind else rnd.choice(MASTERS_OF[kind])
            # 22.11.2026 (брат): «Время на выполнение» — реальное время мастера (бывает быстрее или дольше нормы)
            tm = round((j[2] or 1.0) * rnd.choice((0.75, 0.9, 1.0, 1.2, 1.5)) * 4) / 4 if rnd.random() < 0.85 else ""
            post(o.mgr, f"{o.url}/works/{w.id}", {"name": w.name, "pricing": "fixed" if j[2] is None else "hours",
                                                   "hours": str(j[2] or 0), "rate": str(j[3]), "qty": "1", "time_h": str(tm),
                                                   "mechanic_id": str(uid(master))}, what="Цена, время и исполнитель")
        for kind, j in o.extra:
            post(o.mgr, f"{o.url}/works", {"name": j[1], "pricing": "fixed" if j[2] is None else "hours", "hours": str(j[2] or 0),
                                           "rate": str(j[3]), "qty": "1", "time_h": str(j[2] or 1),
                                           "mechanic_id": str(uid(rnd.choice(MASTERS_OF[kind])))},
                 what=f"Работа второму мастеру ({kind})")
        if o.wo().works_sent_at is None:
            post(o.mgr, f"{o.url}/works-sent", what="Работы записаны → на подбор")
    R.check(o.wo().works_sent_at is not None, "менеджер записал работы → на подбор", o.wo().number)
    o.step = "pick"


def step_pick(o, day, hhmm, send=True):
    """Запчастист на согласовании: со склада (сразу резерв) или «заказать» (закупки ещё нет), иногда — варианты разных
    фирм одной группой; «Подбор завершён» → менеджеру «Подбор готов»."""
    wo = o.wo()
    pm = one(M.User, M.User.id == wo.partsman_id).login if wo.partsman_id else rnd.choice(PARTSMEN[o.mgr])
    with at(day, hhmm):
        for w in works_of(o):
            kind, j = job_by_name(w.name)
            if not j:
                continue
            for name, qty in j[4]:
                cid = cat_id(name)
                with_extra = rnd.random() < 0.08
                post(pm, f"{o.url}/parts", {"catalog_id": str(cid) if cid else "", "name": name, "qty": str(qty),
                                            "cost": "", "markup_pct": str(rnd.choice((30, 35, 40, 45))),
                                            "wo_work_id": str(w.id), "action": "order" if with_extra else "auto",
                                            "order_qty": str(qty + 2) if with_extra else ""}, what=f"Подбор: {name}")
            for name, art, brand, cost in j[5]:
                post(pm, f"{o.url}/parts", {"name": name, "article": art, "brand": brand, "qty": "1", "cost": str(cost),
                                            "markup_pct": "30", "wo_work_id": str(w.id), "action": "order"}, what=f"Заказать: {name}")
        # Варианты на выбор клиенту — одна деталь двух фирм, ОДНОЙ группой («ещё вариант к: …»).
        # 19.11.2026: раньше прогон искал группу неверно — варианты ложились по одному, менеджер «выбирал» оба
        # (наряд 39: три комплекта колодок). Если такие колодки уже взяты со склада — они тоже становятся вариантом.
        if o.kind == "mechanic" and rnd.random() < 0.15:
            base = next((p for p in parts_of(o) if p.name == PADS and p.status == "reserved"), None)
            g = f"part:{base.id}" if base else ""
            for brand, cost in (("Jurid", "1050"), ("Toyota", "2300")):
                post(pm, f"{o.url}/parts", {"name": PADS, "brand": brand, "qty": "1", "cost": cost,
                                            "markup_pct": "35", "action": "option", "option_group": g}, what="Вариант на выбор клиенту")
                grp = next((p.option_group for p in parts_of(o) if p.status == "option" and p.option_group), "")
                g = grp or g
            groups = {p.option_group for p in parts_of(o) if p.status == "option"}
            R.check(len(groups) == 1, "варианты на выбор — одной группой", f"{o.wo().number}: групп {len(groups)}")
        post(pm, f"{o.url}/picking-done", what="Подбор завершён")
    R.check(o.wo().picked_at is not None or o.wo().status != "approve", "«Подбор завершён» отмечен", o.wo().number)
    o.step = "approve"


def step_approve(o, day, hhmm, send=True):
    """Менеджер позвонил клиенту: варианты — «Выбрать», лишнее — в архив, «Согласовал с клиентом». Только теперь
    запчасти уходят в заказ; запчастист — «Заказал» (весь наряд или по строке). send=False — заказ остаётся ему на сегодня."""
    wo = o.wo()
    pm = one(M.User, M.User.id == wo.partsman_id).login if wo.partsman_id else rnd.choice(PARTSMEN[o.mgr])
    with at(day, hhmm):
        groups = {}
        for p in parts_of(o):
            if p.status == "option":
                groups.setdefault(p.option_group, []).append(p)
        for g, items in groups.items():
            post(o.mgr, f"{o.url}/parts/{rnd.choice(items).id}/choose", what="Клиент выбрал вариант")
        if rnd.random() < 0.06:   # клиент передумал — в архив и обратно
            ps = [p for p in parts_of(o) if p.status in ("need", "reserved")]
            if ps:
                post(o.mgr, f"{o.url}/parts/{ps[0].id}/delete", what="Убрать в архив")
                post(o.mgr, f"{o.url}/parts/{ps[0].id}/restore", what="Вернуть из архива")
        # 23.11.2026 (брат): звонок — клиент согласует РАБОТЫ: у каждой «✓ Да» / «✕ Нет»
        ws = q(M.WoWork, M.WoWork.work_order_id == o.id, M.WoWork.approval == "pending", order=M.WoWork.created_at)
        if o.fail_plan in ("all_no", "part_no") and (not ws or o.wo().scenario == "A"):
            o.fail_plan = None   # без диагностики (сценарий А) работы известны заранее — клиент их и заказал
        job_names = {j[1] for j in o.jobs}
        job_ws = [w for w in ws if w.name in job_names]
        for w in ws:
            v = "yes"
            if o.fail_plan == "all_no":
                v = "no"
            elif o.fail_plan == "part_no" and len(job_ws) > 1 and w is job_ws[-1]:
                v = "no"
            elif w.name not in job_names and not any(w.name == j[1] for _, j in o.extra) and rnd.random() < 0.6:
                v = "no"   # лишняя находка — клиент чаще отказывается (уйдёт в рекомендации)
            post(o.mgr, f"{o.url}/works/{w.id}/approval", {"value": v},
                 what="Клиент согласен на работу" if v == "yes" else "Клиент отказался от работы", fetch=True)
        # 25.11.2026 (брат, наряд 39): запчасти — тоже «✓ Да, берём» / «✕ Клиент отказался» (изредка отказ — в карту клиента)
        ps = q(M.WoPart, M.WoPart.work_order_id == o.id, M.WoPart.approval == "pending", order=M.WoPart.created_at)
        for p in ps:
            if p.option_group or p.status in ("archived", "returned", "declined"):
                continue
            v = "no" if (o.fail_plan is None and len(ps) > 2 and rnd.random() < 0.08) else "yes"
            post(o.mgr, f"{o.url}/parts/{p.id}/approval", {"value": v},
                 what="Клиент берёт запчасть" if v == "yes" else "Клиент отказался от запчасти", fetch=True)
        # 19.11.2026: одна деталь несколько раз (две работы с одним маслом) — менеджер подтверждает, что клиент берёт все
        from app.services import orders as _osvc
        with SessionLocal() as db_:
            dups = _osvc.duplicate_parts(db_, db_.get(M.WorkOrder, o.id))
        post(o.mgr, f"{o.url}/approval-done", {"dups_ok": "1"} if dups else {}, what="Согласовал с клиентом"
             + (" (да, клиент берёт все)" if dups else ""))
    st = o.wo().status
    if o.fail_plan == "all_no":
        R.check(st == "ready", "клиент отказался от всего → «Готово» без работ", st)
        o.step = "pay"
        return
    R.check(st in ("instock", "waiting", "work"), "после согласования: на складе / ждём / в работе", f"{wo.number}: {st}")
    if st == "waiting":
        R.check(all(p.created_by == uid(pm) for p in q(M.Purchase, M.Purchase.work_order_id == o.id)),
                "заказ под наряд — на имя запчастиста наряда", wo.number)
        if send:
            order_drafts(o, pm, day, _later(hhmm, 30))
        o.step = "wait_parts"
    elif st == "instock":
        o.step = "issue"
    else:
        o.step = "work"
    if o.fail_plan == "cancel" and o.step in ("wait_parts", "issue"):
        o.step = "cancel"


def order_drafts(o, pm, day, hhmm):
    """Запчастист в «Заказ по наряду» (13.11.2026): один поставщик на весь наряд или у каждой детали свой;
    «Заказал» по детали (с датой «привезут») или «✓ Заказал всё»."""
    sups = ("Элит-Украина", "Автотехнікс", "ОмегаАвто")
    with at(day, hhmm):
        def draft_lines():
            ps = [p.id for p in q(M.Purchase, M.Purchase.work_order_id == o.id, M.Purchase.status == "draft")]
            return q(M.PurchaseLine, M.PurchaseLine.purchase_id.in_(ps)) if ps else []
        exp = str(day + timedelta(days=rnd.choice((1, 2, 2, 3))))
        if rnd.random() < 0.6:
            post(pm, f"/my-orders/wo/{o.id}/supplier", {"supplier_new": rnd.choice(sups)}, what="Один поставщик на весь наряд")
        else:
            for l in draft_lines():
                post(pm, f"/purchase-lines/{l.id}/supplier", {"supplier_new": rnd.choice(sups)}, what="Поставщик у детали")
        if rnd.random() < 0.3:
            for l in draft_lines():
                post(pm, f"/purchase-lines/{l.id}/ordered", {"expected_on": exp}, what="Заказал (деталь)")
        else:
            post(pm, f"/my-orders/wo/{o.id}/ordered", {"expected_on": exp}, what="✓ Заказал всё")
        R.check(not draft_lines(), "после «Заказал» по наряду не осталось черновиков", str(o.id))
        if rnd.random() < 0.3:
            urg = [p for p in parts_of(o) if p.status == "ordered"]
            if urg:
                post(o.lead, f"{o.url}/parts/{urg[0].id}/urgent", what="🔥 Срочно", fetch=True)


def deliveries(day):
    """Кладовщик: что привезли сегодня — принять (иногда неполная поставка)."""
    for p in q(M.Purchase, M.Purchase.status.in_(("ordered", "partial"))):
        if p.kind == "chem":
            continue
        due = p.expected_on or day
        if due <= day:
            with at(day, f"{rnd.randint(10, 15)}:{rnd.randint(0, 59):02d}"):
                receive(str(p.id), partial=p.status == "ordered" and rnd.random() < 0.3)


def step_issue(o, day, hhmm):
    with at(day, hhmm):
        post(o.lead, f"{o.url}/issue-request", {"comment": rnd.choice(("", "", "к 12:00"))}, what="Запросить выдачу")
    reqs = q(M.IssueRequest, M.IssueRequest.work_order_id == o.id, M.IssueRequest.status == "new")
    with at(day, _later(hhmm, 20)):
        for r in reqs:
            keeper = "dmitro" if r.zone == "razbor" else "pavel"   # 20.11.2026: б/у выдаёт кладовщик разбора
            if rnd.random() < 0.5:
                post(keeper, f"/stock/issues/{r.id}/ready", what="📦 Готово к выдаче")
                post(o.mgr if rnd.random() < 0.5 else keeper, f"/stock/issues/{r.id}/issue", {"back": "/ready"}, what="Выдано (забрал)")
            else:
                post(keeper, f"/stock/issues/{r.id}/issue", {"back": "/stock/issues"}, what="Выдано сразу")
    st = o.wo().status
    R.check(st == "work", "после выдачи запчастей → «В работе»", f"{o.wo().number}: {st}")
    if day == TODAY and not LEFT_RECV:
        # 20.11.2026: на сегодня — выдали, слесарь ещё не нажал «✓ Получил» (🟢 у него, «Начал» серая)
        LEFT_RECV.append(o.id)
        page = get(o.lead, o.url)
        R.check(page is not None and "Сначала подтвердите" in page.text, "не подтвердил «Получил» — «Начал» серая",
                o.wo().number)
        o.step = "recv_wait"
        return
    with at(day, _later(hhmm, 25)):
        confirm_parts(o)
    o.step = "work"


LEFT_RECV: list = []


def _one_per_lead(orders, n=3):
    """По одному наряду на мастера (слесарь, моторист, электрик), чтобы инструмент был у разных людей."""
    out, seen = [], set()
    for o in orders:
        if o.lead not in seen and o.lead in MECHS + ["elektro", "motor"]:
            seen.add(o.lead)
            out.append(o)
    return out[:n]


def parts_owners(o, r):
    """Кто подтверждает «Получил» (как в CRM, services/receipt.owners): мастер работы детали, иначе ведущий."""
    lead = o.wo().mechanic_id
    out = set()
    for line in q(M.IssueRequestLine, M.IssueRequestLine.request_id == r.id):
        p = one(M.WoPart, M.WoPart.id == line.wo_part_id)
        w = one(M.WoWork, M.WoWork.id == p.wo_work_id) if p and p.wo_work_id else None
        out.add((w.mechanic_id if w and w.mechanic_id else None) or lead)
    return {one(M.User, M.User.id == u).login for u in out if u} or {o.lead}


def confirm_parts(o):
    """20.11.2026 (брат): выдали — мастер нажимает «✓ Получил» (до этого «Начал» серая)."""
    for r in q(M.IssueRequest, M.IssueRequest.work_order_id == o.id, M.IssueRequest.status == "issued",
               M.IssueRequest.received_at.is_(None)):
        for login in sorted(parts_owners(o, r)):
            if one(M.IssueRequest, M.IssueRequest.id == r.id).received_at:
                break
            page = get(login, o.url)
            R.check(page is not None and "✓ Получил" in page.text, "выдали — у мастера кнопка «✓ Получил»", o.wo().number)
            post(login, f"/stock/issues/{r.id}/received", {"back": o.url}, what="✓ Получил запчасти")


def step_work(o, day, hhmm):
    """Каждый мастер — свои работы: Начал → (Пауза с причиной → Продолжить) → Выполнено."""
    t = hhmm
    lead = o.wo().mechanic_id
    owner = lambda w: one(M.User, M.User.id == (w.mechanic_id or lead))   # noqa: E731
    # 19.11.2026: мойка — после ремонта (сначала работы слесарей, потом мойщика / детейлера)
    for w in sorted(works_of(o), key=lambda w: (owner(w).login in WASHERS_L) if owner(w) else False):
        if w.status == "done":
            continue
        who = owner(w)
        if not who:
            R.check(False, "у работы есть исполнитель", f"{o.wo().number}: {w.name}")
            continue
        login = who.login
        if login in WASHERS_L and one(M.WoWork, M.WoWork.id == w.id).status == "todo" and not wash_ready(login, o, day, t):
            continue
        with at(day, t):
            post(login, f"{o.url}/works/{w.id}/state", {"action": "start"}, what=f"Начал: {w.name}")
            if login in ("moyka", "detail") and rnd.random() < 0.6:
                chem_ask(login, o)
            if login in MECHS + ["elektro", "motor"] and rnd.random() < 0.08:
                tool_flow(login, o)
            if rnd.random() < 0.12:
                post(login, f"{o.url}/works/{w.id}/state", {"action": "pause", "reason": rnd.choice(("Обед", "Ждём деталь", "Позвали на другую машину"))},
                     what="Пауза с причиной")
        t = _later(t, rnd.randint(20, 90))
        if rnd.random() < 0.06 and who.role.code in ("mechanic", "electrician", "motorist"):   # нашли новое во время ремонта
            with at(day, t):
                post(login, f"{o.url}/findings", {"text": "Во время ремонта: течь сальника привода", "urgency": "soon"},
                     what="Новая находка во время ремонта", fetch=True)
            R.check(o.wo().status == "approve", "находка во время работы → снова «Согласование»", o.wo().status)
            with at(day, _later(t, 15)):   # 23.11.2026: круг заново — работа к новому, без запчастей, клиент отказался
                post(o.mgr, f"{o.url}/works", {"name": "Замена сальника привода", "pricing": "fixed", "rate": "600", "qty": "1",
                                               "time_h": "1"}, what="Работа к новой проблеме")
                post(o.mgr, f"{o.url}/works-sent", {"no_parts": "1"}, what="Работы записаны — запчасти не нужны")
                for nw in q(M.WoWork, M.WoWork.work_order_id == o.id, M.WoWork.approval == "pending"):
                    post(o.mgr, f"{o.url}/works/{nw.id}/approval", {"value": "no"}, what="Клиент отказался от новой", fetch=True)
                post(o.mgr, f"{o.url}/approval-done", what="Согласование завершено")
        with at(day, t):
            w2 = one(M.WoWork, M.WoWork.id == w.id)
            if w2.status == "paused":
                post(login, f"{o.url}/works/{w.id}/state", {"action": "start"}, what="Продолжить")
            if rnd.random() < 0.05:
                post(login, "/messages", {"order_id": str(o.id), "to": "manager", "text": "Нужно согласовать доп. работы по ходовой",
                                          "back": o.url}, what="Сообщение менеджеру")
            post(login, f"{o.url}/works/{w.id}/state", {"action": "done"}, what=f"Выполнено: {w.name}")
        t = _later(t, 10)
    st = o.wo().status
    if o.fail_plan == "return_part":
        issued = [p for p in parts_of(o) if p.status in ("issued", "installed")]
        if issued:
            with at(day, t):
                post("pavel", f"{o.url}/parts/{issued[0].id}/return", what="Деталь не подошла — вернули на склад")
    R.check(st in ("ready", "paid"), "все работы выполнены → «Готово»", f"{o.wo().number}: {st}")
    o.step = "pay"


def step_pay(o, day, hhmm):
    from app.services import money as msvc
    with at(day, hhmm):
        if o.fail_plan == "handover":
            other = "marina" if o.mgr == "irina" else "irina"
            wo = o.wo()
            post(o.mgr, f"{o.url}/assign", {"partsman_id": str(wo.partsman_id or ""), "mechanic_id": str(wo.mechanic_id or ""),
                                            "lift_id": "", "manager_id": str(uid(other))}, what="Передать наряд другому менеджеру")
            post(other, f"{o.url}/handover-ack", what="✓ Принял наряд")
            o.mgr = other
        with SessionLocal() as db:
            due = msvc.totals(db, db.get(M.WorkOrder, o.id)).due
        if due > 0:
            if rnd.random() < 0.1:   # часть наличными, остальное картой
                half = (due / 2).quantize(Decimal("1"))
                post(o.mgr, f"{o.url}/payments", {"amount": str(half), "method": "cash", "purpose": "extra", "token": f"t{rnd.random()}"},
                     what="Частичная оплата")
                due -= half
            post(o.mgr, f"{o.url}/payments", {"amount": str(due), "method": rnd.choice(("cash", "card", "terminal", "terminal")),
                                              "purpose": "full", "receipt_no": str(rnd.randint(1000, 9999)), "token": f"t{rnd.random()}"},
                 what="Принять оплату")
            # двойное нажатие той же формы — не должно задвоить
        with SessionLocal() as db:
            t = msvc.totals(db, db.get(M.WorkOrder, o.id))
        R.check(t.due == 0, "после оплаты остаток 0", f"{o.wo().number}: остаток {t.due}")
        adv = rnd.random() < 0.2
        post(o.mgr, f"{o.url}/close", {"state": "advice" if adv else "ok", "defects": "",
                                       "advice": "Задние колодки ~40 % — заменить при следующем ТО" if adv else ""}, what="Выдать авто")
    st = o.wo().status
    R.check(st == "closed", "после выдачи наряд закрыт", f"{o.wo().number}: {st}")
    o.step = "done"
    if o.fail_plan == "reopen" and st == "closed":
        o.step = "reopen"


def step_reopen(o, day, hhmm):
    if (TODAY - day).days > 2:   # прогон идёт «сейчас»: наряд старше 3 дней менеджер уже не видит — не открываем
        o.step = "done"
        return
    with at(day, hhmm):
        post(o.mgr, f"{o.url}/reopen-request", {"reason": "Клиент вернулся: стук остался — гарантия"}, what="Запрос «Открыть заново»")
        r = one(M.OrderReopen, M.OrderReopen.work_order_id == o.id, M.OrderReopen.status == "pending")
        if r:
            post("director", f"/reopen-requests/{r.id}", {"action": "approve"}, what="Директор: открыть заново")
    st = o.wo().status
    R.check(st not in ("closed",), "после разрешения наряд открыт заново", st)
    with at(day, _later(hhmm, 60)):
        post(o.mgr, f"{o.url}/works", {"name": "Гарантия: протяжка подвески", "pricing": "fixed", "rate": "0", "qty": "1",
                                       "mechanic_id": str(uid(o.lead))}, what="Гарантийная работа (0 ₴)")
    o.step = "work"
    o.fail_plan = None


def side_works(o, day, hhmm):
    """Пока ведущий ждёт запчасти, мойщик / электрик делают свои работы (12.11.2026)."""
    wo = o.wo()
    for w in works_of(o):
        if w.status == "done" or not w.mechanic_id or w.mechanic_id == wo.mechanic_id:
            continue
        # под работу нужны запчасти, а они ещё не выданы — мастер её не начинает (CRM и не даст)
        if any(p.wo_work_id == w.id and p.status not in ("issued", "installed", "declined", "option", "returned")
               for p in parts_of(o)):
            continue
        who = one(M.User, M.User.id == w.mechanic_id)
        if not who or rnd.random() < 0.5:
            continue
        # 19.11.2026: мойщик / детейлер — после слесарей; раньше — только если менеджер разрешил («Можно мыть сейчас»)
        if who.login in WASHERS_L and (rnd.random() < 0.6 or not wash_ready(who.login, o, day, hhmm)):
            continue
        with at(day, hhmm):
            r = post(who.login, f"{o.url}/works/{w.id}/state", {"action": "start"}, what=f"Пока ждём запчасти — начал: {w.name}")
            post(who.login, f"{o.url}/works/{w.id}/state", {"action": "done"}, what=f"Пока ждём запчасти — выполнено: {w.name}")
        R.check(one(M.WoWork, M.WoWork.id == w.id).status == "done", "второй мастер сделал работу, пока ждём запчасти", w.name)
        R.check(o.wo().status == wo.status, "работа второго мастера не меняет этап наряда", f"{wo.status} → {o.wo().status}")


def step_cancel(o, day, hhmm):
    from app.services import money as msvc
    with SessionLocal() as db:
        total = msvc.totals(db, db.get(M.WorkOrder, o.id)).total
    pre = str(min(Decimal(1000), (total / 2).quantize(Decimal("1"))))
    with at(day, hhmm):
        if rnd.random() < 0.5 and Decimal(pre) > 0:
            post(o.mgr, f"{o.url}/payments", {"amount": pre, "method": "cash", "purpose": "prepay", "token": f"t{rnd.random()}"},
                 what="Предоплата")
            post(o.mgr, f"{o.url}/refund", {"amount": pre, "method": "cash", "reason": "Клиент передумал ремонтировать"},
                 what="Возврат денег клиенту")
        post(o.mgr, f"{o.url}/cancel", {"reason": "Клиент забрал машину без ремонта", "keep_money": ""}, what="Отменить наряд")
    st = o.wo().status
    R.check(st == "cancel", "наряд отменён", st)
    with SessionLocal() as db:
        res = db.scalar(select(func.count()).select_from(M.WoPart).where(M.WoPart.work_order_id == o.id, M.WoPart.status == "reserved"))
    R.check(not res, "отмена снимает резервы", f"в резерве осталось {res}")
    o.step = "done"


# ================================================================ мойка, инструмент, разбор, табель, зарплата
def chem_ask(login, o=None):
    items = [c for c in q(M.PartCatalog, M.PartCatalog.section == "chem")]
    if not items:
        return
    c = rnd.choice(items)
    post(login, "/wash/ask", {"catalog_id": str(c.id), "qty": "1", "order_id": str(o.id) if o else "",
                              "note": rnd.choice(("", "закончилась", "для салона"))}, what=f"Химия: прошу {c.name}")


# ---------------------------------------------------------------- 19.11.2026: одна очередь мойки
WASHERS_L = ("moyka", "detail")
QW_SETS = [  # быстрая мойка: (услуги, кто моет — None = любой мойщик)
    (["Экспресс-мойка (поверхностная)"], None), (["Мойка кузова (бесконтактная)", "Чернение шин"], None),
    (["Комплексная мойка (кузов + салон)"], None), (["Мойка кузова ручная", "Пылесос салона"], None),
    (["Мойка кузова (бесконтактная)", "Мойка дисков", "Мойка ковриков"], None),
    (["Химчистка салона"], "detail"), (["Полировка кузова"], "detail"), (["Озонирование салона"], "detail"),
]


def wash_err(login, o):
    """Что видит мойщик: можно ли ему сейчас начать эту машину (по очереди), или почему нельзя."""
    from app.services import wash as wsvc
    with SessionLocal() as db:
        u = db.scalar(select(M.User).where(M.User.login == login))
        wo = db.get(M.WorkOrder, o.id)
        return wsvc.start_error(db, wo.branch_id, wo, u)


def wash_ready(login, o, day, hh):
    """Мойщик берёт машины строго по очереди. Не первая — менеджер ставит её первой (так и в жизни: машину ждут);
    машина с ремонта, где слесари ещё работают, — «Можно мыть сейчас»."""
    err = wash_err(login, o)
    if not err:
        return True
    with at(day, hh):
        if "идёт ремонт" in err:
            post(o.mgr, f"/wash/{o.id}/wash-now", what="Мойка: можно мыть сейчас")
        if wash_err(login, o):
            post(o.mgr, f"/wash/{o.id}/move", {"where": "first"}, what="Мойка: поставить машину первой")
    err = wash_err(login, o)
    R.check(not err, "мойщик может начать машину, когда она первая для него", f"{o.wo().number}: {err}")
    return not err


def quick_wash(day, hh, first=False, services=None, washer=None):
    """Быстрая приёмка на мойку (менеджер): услуги из прайса, класс машины; «первой» — вне очереди."""
    c = free_client()
    if services is None:
        services, washer = rnd.choice(QW_SETS)
    ids = [s.id for s in q(M.WashService, M.WashService.name.in_(services))]
    if not ids:
        R.check(False, "услуги мойки есть в прайсе", ", ".join(services))
        return None
    token = f"sim-qw-{rnd.random()}"
    data = {"phone": c["phone"], "name": c["name"], "plate": c["plate"], "make": c["make"], "model": c["model"],
            "car_class": rnd.choices(("car", "suv", "bus"), weights=(6, 4, 1))[0], "washer_id": str(uid(washer)) if washer else "",
            "fuel_level": rnd.choice(("25", "50", "75")), "intake_token": token, "queue_place": "first" if first else "",
            "note": rnd.choice(("", "", "Коврики не мыть", "Позвонить, когда будет готово"))}
    with at(day, hh):
        r = post("irina", "/wash/new", data, files=[("service_id", (None, str(i))) for i in ids] + photo(1),
                 what="Приёмка на мойку" + (" — первой" if first else ""), fetch=True)
    wo = one(M.WorkOrder, M.WorkOrder.intake_token == token)
    if r is None or wo is None:
        return None
    o = Order(c, "irina", "quick", [], "A", washer or "moyka", [])
    o.id, o.step, o.last, o.born = wo.id, "qwash", (day, hh), day
    ACTIVE.append(o)
    if first:
        from app.services import wash as wsvc
        with SessionLocal() as db:
            qq = wsvc.board(db, wo.branch_id)["queue"]
        R.check(qq and qq[0].o.id == wo.id, "быстрая мойка «первой» — первая в очереди",
                wo.number + " · очередь: " + ", ".join(f"{c.o.number[-3:]}(r{c.o.queue_rank}{' серая' if c.blocked else ''})"
                                                       for c in qq[:6]))
    return o


def wash_today(day, skip=()):
    """19.11.2026: на сегодня в очереди мойки есть всё, что проверить руками: мойщик моет машину; в очереди быстрая
    мойка (одна поставлена «первой»); полировка детейлера; машина с ремонта — слесарь закончил, ждёт мойки; запросы
    мойщика «Попросить первой» — один согласован, один отказан, один ждёт менеджера (красная плитка)."""
    from app.services import wash as wsvc
    o1 = quick_wash(day, "13:20", services=["Комплексная мойка (кузов + салон)"])
    if o1:
        step_qwash(o1, day, "13:40", finish=False)   # моет сейчас — «Выполнено» руками
    a = quick_wash(day, "14:30", services=["Мойка кузова (бесконтактная)", "Чернение шин"])
    d = quick_wash(day, "14:50", services=["Полировка кузова"], washer="detail")
    b = quick_wash(day, "15:10", services=["Экспресс-мойка (поверхностная)"])
    # машина с ремонта: слесарь всё сделал, осталась мойка — в очереди наравне со всеми
    rep = next((o for o in ACTIVE if o.id not in skip and o.kind == "mechanic" and o.wo() and o.wo().status == "work"
                and o.step != "work_wait"), None)
    if rep:
        with at(day, "15:20"):
            post(rep.mgr, f"{rep.url}/masters", {"user_id": str(uid("moyka")), "work": "Комплексная мойка после ремонта"},
                 what="Добавить мастера: мойщик (после ремонта)")
        t = "15:30"
        for w in works_of(rep):
            who = one(M.User, M.User.id == (w.mechanic_id or rep.wo().mechanic_id))
            if w.status == "done" or not who or who.login in WASHERS_L:
                continue
            with at(day, t):
                if w.status != "doing":
                    post(who.login, f"{rep.url}/works/{w.id}/state", {"action": "start"}, what=f"Начал: {w.name}")
                post(who.login, f"{rep.url}/works/{w.id}/state", {"action": "done"}, what=f"Выполнено: {w.name}")
            t = _later(t, 20)
        rep.step = "wash_wait"
        with SessionLocal() as db:
            c = next((c for c in wsvc.board(db, rep.wo().branch_id)["queue"] if c.o.id == rep.id), None)
        R.check(c is not None and not c.blocked, "машина с ремонта: слесарь закончил — в очереди мойки, мыть можно",
                rep.wo().number)
    c = quick_wash(day, "15:45", services=["Мойка кузова ручная", "Пылесос салона"], first=True)
    # запросы мойщика «Попросить первой»
    asks = [(a, "Клиент уже приехал, ждёт в зале", "ok"), (b, "Экспресс — 15 минут, клиент спешит", "no"),
            (rep or b, "Клиент заберёт через час — просит помыть", "")]
    for i, (o, reason, answer) in enumerate(asks):
        if not o:
            continue
        hh = ("15:50", "16:05", "16:30")[i]
        with at(day, hh):
            post("moyka", f"/wash/{o.id}/ask", {"reason": reason}, what="Мойщик: попросить первой")
        r = one(M.WashQueueRequest, M.WashQueueRequest.work_order_id == o.id, M.WashQueueRequest.status == "new")
        R.check(r is not None, "запрос мойщика «изменить очередь» дошёл до менеджера", o.wo().number)
        if r and answer:
            with at(day, _later(hh, 5)):
                post("irina", f"/wash/asks/{r.id}", {"ok": "1" if answer == "ok" else "",
                                                     "answer": "" if answer == "ok" else "Клиент заберёт только вечером"},
                     what="Менеджер: " + ("согласен — первой" if answer == "ok" else "отказать"))
            R.check(one(M.WashQueueRequest, M.WashQueueRequest.id == r.id).status == ("ok" if answer == "ok" else "declined"),
                    "менеджер ответил на запрос мойщика", answer)
    for o in (a, b, c, d):
        if o:
            o.step = "qwash_today"   # мыть — руками


def check_badges():
    """19.11.2026 (брат): у КАЖДОГО сотрудника на сегодня есть дело — горит хотя бы одна цифра в меню."""
    for login in NAMES:
        r = get(login, "/api/notify", what="Цифры меню")
        b = r.json().get("badges", {}) if r is not None and r.status_code == 200 else {}
        R.check(bool(b), "у каждого сотрудника на сегодня горит цифра (есть что сделать)", login)
        BADGES[login] = {k: [f"{x['c']} {x['n']} — {x['t']}" for x in v] for k, v in b.items()}


BADGES = {}


def step_qwash(o, day, hhmm, finish=True):
    """Мойщик: «Взять следующую» (машина первая для него) → услуги «Выполнено» → наряд «Готово»."""
    login = o.lead
    if not wash_ready(login, o, day, hhmm):
        return
    with at(day, hhmm):
        r = post(login, "/wash/take", {"order_id": str(o.id)}, what="🫧 Взять следующую")
    R.check(r is not None and str(r.url).endswith(str(o.id)), "мойщик взял свою машину по очереди", o.wo().number)
    if rnd.random() < 0.4:
        with at(day, _later(hhmm, 5)):
            chem_ask(login, o)
    if not finish:
        o.step = "qwash_doing"
        return
    t = _later(hhmm, rnd.randint(15, 60))
    for w in works_of(o):
        if w.status == "done":
            continue
        with at(day, t):
            if w.status == "todo":
                post(login, f"{o.url}/works/{w.id}/state", {"action": "start"}, what=f"Начал: {w.name}")
            post(login, f"{o.url}/works/{w.id}/state", {"action": "done"}, what=f"Выполнено: {w.name}")
        t = _later(t, rnd.randint(10, 40))
    R.check(o.wo().status == "ready", "помыли — наряд мойки «Готово»", f"{o.wo().number}: {o.wo().status}")
    o.step = "pay"


def chem_desk(day):
    """Кладовщик раз в день отвечает на запросы химии."""
    reqs = q(M.ChemRequest, M.ChemRequest.status == "new")
    if not reqs:
        return
    with at(day, "16:30"):
        for r in reqs:
            if rnd.random() < 0.1 or free_qty(r.catalog_id) < r.qty:
                post("pavel", f"/chem/{r.id}/decline", {"reason": "Закончилась — заказали"}, what="Отказать в химии")
            else:
                before = free_qty(r.catalog_id)
                if rnd.random() < 0.5:   # 19.11.2026: «📦 Готово к выдаче» — отложили, мойщик пришёл → «Выдано»
                    post("pavel", f"/chem/{r.id}/ready", {"qty": str(r.qty)}, what="Химия: готово к выдаче")
                    R.check(free_qty(r.catalog_id) == before - r.qty, "химия «готово к выдаче» отложена (резерв)", str(r.catalog_id))
                    post("pavel", f"/chem/{r.id}/issue", {}, what="Химия: выдано (пришли)")
                else:
                    post("pavel", f"/chem/{r.id}/issue", {"qty": str(r.qty)}, what="Выдать химию сразу")
                R.check(free_qty(r.catalog_id) == before - r.qty, "выданная химия уменьшила остаток", str(r.catalog_id))


def tool_flow(login, o):
    tools = [t for t in q(M.Tool, M.Tool.category.notin_(("personal", "spare"))) if not t.holder_id]
    if not tools:
        return
    t = rnd.choice(tools)
    post(login, f"{o.url}/tool-requests", {"tool_id": str(t.id), "comment": "под эту машину"}, what=f"Попросить инструмент «{t.name}»")
    r = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
    if r:
        if rnd.random() < 0.5:   # 19.11.2026: «📦 Готово к выдаче» → слесарь пришёл → «Выдано»
            post("pavel", f"/tool-requests/{r.id}/ready", what="Инструмент: готово к выдаче")
        post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать инструмент")
        post(login, f"/tool-requests/{r.id}/received", what="✓ Получил инструмент")   # 20.11.2026
        if rnd.random() < 0.35:   # 20.11.2026: второй способ — принесли к окошку, кладовщик «Вернул»
            post("pavel", f"/tools/{t.id}/returned", what="Инструмент принесли — «Вернул»")
            R.check(one(M.Tool, M.Tool.id == t.id).holder_id is None, "«Вернул» — инструмент в шкафу", t.name)
            return
        post(login, f"/tools/{t.id}/return-request", what="Сдаю инструмент")
        r2 = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
        if r2:
            post("pavel", f"/tool-requests/{r2.id}/done", {"result": "ok"}, what="Принял инструмент в шкаф")


def tool_check(day):
    with at(day, "17:30"):
        u = one(M.User, M.User.login == "andrey")
        r = post("pavel", f"/tools/staff/{u.id}/checks", {"kind": "planned"}, what="Проверка инструмента")
        c = one(M.ToolCheck, M.ToolCheck.user_id == u.id, M.ToolCheck.status == "draft")
        if not c:
            return
        lines = q(M.ToolCheckLine, M.ToolCheckLine.check_id == c.id)
        data = {"action": "finish"}
        for i, l in enumerate(lines):
            data[f"r_{l.id}"] = "lost" if i == 0 else "ok"
        post("pavel", f"/tools/checks/{c.id}", data, what="Завершить проверку (один ключ утерян)")
        post("admin", f"/tools/checks/{c.id}/approve", {"approve": [str(l.id) for l in lines[:1]]}, what="Утвердить удержание")


DONORS = [  # 19.11.2026: три машины на разборе — детали списком (категория угадывается сама) и по одной (с категорией)
    # 20.11.2026: новая имитация — другие доноры и детали
    ("Hyundai", "Tucson TL", "2017", "TMAJ3815AHJ371206", "176000", "118000",
     ("Фара левая LED; 6800", "Фара правая LED; 6800", "Дверь передняя правая; 7200", "Крышка багажника; 6400",
      "Блок SRS; 2900", "Генератор 150 А; 3100", "Стартер; 2100", "Зеркало левое с подогревом; 1900",
      "Коробка АКПП A6MF1; 31000", "Двигатель G4NA 2.0 в сборе; 58000")),
    ("Ford", "Focus III", "2013", "WF0KXXGCBKDS40217", "254000", "64000",
     ("Бампер задний; 3200", "Крыло переднее левое; 2100", "Фонарь задний правый; 1600", "Радиатор кондиционера; 1900",
      "Компрессор кондиционера; 4800", "Блок управления двигателем; 4200", "Сиденье пассажира; 2900", "Капот; 3800")),
    ("Mazda", "CX-5 KE", "2015", "JMZKE17F200304911", "201000", "121000",
     ("Руль с подушкой; 5600", "Панель приборов; 3600", "Стеклоподъёмник задний левый; 1100", "Амортизатор задний; 1300",
      "Рычаг передний левый; 1250", "Суппорт задний правый; 1400", "Дверь задняя левая; 6100")),
]


def razbor_add_donor(day, hh, spec):
    make, model, year, vin, km, cost, lines = spec
    with at(day, hh):
        post("dmitro", "/razbor/donors", {"make": make, "model": model, "year": year, "vin": vin, "mileage": km,
                                          "purchase_price": cost}, files=photo(1), what=f"Донор {make} {model}")
    d = one(M.Donor, M.Donor.vin == vin)
    if not d:
        return None
    with at(day, _later(hh, 30)):
        post("dmitro", f"/razbor/donors/{d.id}/parts/bulk", {"lines": "\n".join(lines), "condition": "good", "catsub": ""},
             what="Детали донора списком (категория — сама)")
        # одна деталь — формой «＋ Деталь»: подсказка категории по названию, фото
        g = get("dmitro", "/api/razbor/guess?name=Фара противотуманная левая", what="Подсказка категории")
        cs = g.json().get("catsub", "") if g is not None and g.status_code == 200 else ""
        R.check(bool(cs), "категория детали угадывается по названию", "Фара противотуманная левая")
        post("dmitro", f"/razbor/donors/{d.id}/parts", {"name": "Фара противотуманная левая", "price": "650", "qty": "1",
                                                        "condition": "good", "catsub": cs, "publish": "1"},
             files=photo(1), what="＋ Деталь (с категорией)")
    items = q(M.StockItem, M.StockItem.donor_id == d.id)
    cats = {i.id: one(M.PartCatalog, M.PartCatalog.id == i.catalog_id) for i in items}
    other = [c.name for c in cats.values() if not c.category or c.category == "Прочее"]
    R.check(len(other) <= 2, "детали разбора разложены по категориям (почти без «Прочее»)", ", ".join(other[:5]))
    R.check(all(i.price_state == "pending" for i in items), "новые детали разбора — «Не утверждено»", d.code)
    return d


def razbor_decide(day, hh, d, decline=1, edit=1):
    """Директор: «Цены разбора» → по машине: одну цену поправил, одну отказал (с причиной), остальные утвердил."""
    reqs = [r for r in q(M.RazborPrice, M.RazborPrice.status == "pending", M.RazborPrice.kind == "new")
            if one(M.StockItem, M.StockItem.id == r.stock_item_id).donor_id == d.id]
    with at(day, hh):
        get("director", f"/razbor/prices?donor={d.id}", what="Цены разбора по машине")
        for i, r in enumerate(reqs):
            if i < decline:
                post("director", f"/razbor/prices/{r.id}/decline", {"answer": "Дорого — на OLX такие по 70 %"},
                     what="Отказать в цене")
            elif i < decline + edit:
                post("director", f"/razbor/prices/{r.id}/approve", {"price": str((r.price * Decimal("0.9")).quantize(Decimal("1")))},
                     what="Утвердить с правкой цены")
        if len(reqs) > decline + edit:
            post("director", f"/razbor/prices/donor/{d.id}/approve-all", {}, what="Утвердить все по машине")
    return reqs[:decline]


def razbor_start(day):
    # 18–19.11.2026: детали разбора с категориями, цены утверждает директор — до этого их не продать
    for n, spec in enumerate(DONORS[:2]):
        d = razbor_add_donor(day, f"1{n}:00", spec)
        if d:
            declined = razbor_decide(day, f"1{n + 2}:00", d)
            for r in declined:   # кладовщик поправил цену и отправил ещё раз → утвердили
                with at(day, f"1{n + 3}:00"):
                    it_ = one(M.StockItem, M.StockItem.id == r.stock_item_id)
                    post("dmitro", f"/razbor/parts/{it_.id}", {"price": str((r.price * Decimal("0.75")).quantize(Decimal("1"))),
                                                               "condition": it_.condition or "good", "publish": "1",
                                                               "description": it_.description or "",
                                                               "location_id": str(it_.location_id or "")}, what="Новая цена после отказа")
                    post("dmitro", f"/razbor/parts/{r.stock_item_id}/resubmit", {"note": "Снизил до рынка"}, what="Отправить ещё раз")
                r2 = one(M.RazborPrice, M.RazborPrice.stock_item_id == r.stock_item_id, M.RazborPrice.status == "pending")
                R.check(r2 is not None and r2.repeat, "после отказа — «Отправить ещё раз» снова у директора", str(r.stock_item_id))
                if r2:
                    with at(day, f"1{n + 4}:00"):
                        post("deputy", f"/razbor/prices/{r2.id}/approve", {}, what="Зам утвердил повторную")
    # Запрос изменения утверждённой цены → зам утвердил
    it = next((i for i in q(M.StockItem, M.StockItem.donor_id.is_not(None)) if not (i.price_state or "")), None)
    if it:
        with at(day, "16:30"):
            post("dmitro", f"/razbor/parts/{it.id}/price-request", {"price": str((it.sale_price * Decimal("1.15")).quantize(Decimal("1"))),
                                                                   "note": "Спрашивают часто — поднять"}, what="Запросить изменение цены")
            r = one(M.RazborPrice, M.RazborPrice.stock_item_id == it.id, M.RazborPrice.kind == "change", M.RazborPrice.status == "pending")
            if r:
                post("deputy", f"/razbor/prices/{r.id}/approve", {}, what="Изменение цены утверждено")
        R.check(one(M.StockItem, M.StockItem.id == it.id).sale_price > it.sale_price, "изменение цены разбора применилось", it.code)
    for v in ("cars", "parts"):
        r = get("dmitro", f"/razbor?v={v}", what=f"Разбор: вкладка {v}")
        R.check(r is not None and r.status_code == 200, "разбор открывается (машины / все детали)", v)


def razbor_today(day):
    """На сегодня: новая машина — цены ждут директора (🔴 у директора и зама), одна деталь отказана (🔴 у кладовщика
    разбора), запрос изменения цены ждёт решения."""
    d = razbor_add_donor(day, "10:30", DONORS[2])
    if d:
        reqs = [r for r in q(M.RazborPrice, M.RazborPrice.status == "pending", M.RazborPrice.kind == "new")
                if one(M.StockItem, M.StockItem.id == r.stock_item_id).donor_id == d.id]
        if reqs:
            with at(day, "12:00"):
                post("director", f"/razbor/prices/{reqs[0].id}/decline", {"answer": "Руль с подушкой — только после проверки подушки"},
                     what="Отказать в цене (кладовщику — посмотреть причину)")
    it = next((i for i in q(M.StockItem, M.StockItem.donor_id.is_not(None)) if not (i.price_state or "")
               and not one(M.UsedSale, M.UsedSale.stock_item_id == i.id)), None)
    if it:
        with at(day, "12:30"):
            post("dmitro", f"/razbor/parts/{it.id}/price-request", {"price": str((it.sale_price * Decimal("0.85")).quantize(Decimal("1"))),
                                                                   "note": "Лежит 2 недели — снизить"}, what="Запросить изменение цены (решить руками)")


def razbor_sale(day, stop=""):
    """stop: "reserve" — только резерв (подготовить), "pack" — упаковано, ждёт отправки / самовывоза,
    "ship" — отправлено, ждём, что получат. Пусто — до конца."""
    items = [i for i in q(M.StockItem, M.StockItem.donor_id.is_not(None))]
    sold = {s.stock_item_id for s in q(M.UsedSale)}
    left = [i for i in items if i.id not in sold and not (i.price_state or "")]   # продать можно только утверждённую цену
    if left:   # 20.11.2026: деталь, которую слесарь взял под наряд (резерв), покупателю не продаётся
        from app.services import stock as _ss
        with SessionLocal() as _db:
            bal = _ss.balances(_db, left[0].branch_id, [i.id for i in left])
        left = [i for i in left if bal.get(i.id, {}).get("free", 0) > 0]
    if not left:
        return
    i = rnd.choice(left)
    c = rnd.choice(CLIENTS)
    np = rnd.random() < 0.6 if stop in ("", "reserve") else stop == "ship"
    with at(day, "13:00"):
        post("dmitro", f"/razbor/parts/{i.id}/sell", {"action": "reserve", "qty": "1", "price": str(i.sale_price or 2000),
                                                      "buyer_name": c["name"], "buyer_phone": c["phone"],
                                                      "delivery": "np" if np else "pickup", "np_address": "Киев, отд. 12" if np else "",
                                                      "cod": "1" if np and rnd.random() < 0.5 else "", "channel": "olx"},
             what="Разбор: резерв под покупателя")
    s = one(M.UsedSale, M.UsedSale.stock_item_id == i.id, M.UsedSale.status == "reserved")
    if not s or stop == "reserve":
        return
    carrier = one(M.Carrier, M.Carrier.name.like("Нова%")) if np else None
    with at(day, "15:00"):
        post("dmitro", f"/razbor/sales/{s.id}", {"action": "pack", "place": "Полка выдачи 1",
                                                  "carrier_id": str(carrier.id) if carrier else ""}, what="Упаковано")
        if np and stop != "pack":
            post("dmitro", f"/razbor/sales/{s.id}", {"action": "ship", "ttn": f"2045{rnd.randint(10**10, 10**11 - 1)}", "paid": ""},
                 what="Отправлено (ТТН)")
    if stop:
        return
    with at(day + timedelta(days=2), "12:00") if day + timedelta(days=2) < TODAY else at(day, "18:00"):
        if np and rnd.random() < 0.15:
            post("dmitro", f"/razbor/sales/{s.id}", {"action": "return", "note": "Не забрал на почте"}, what="Возврат посылки")
        else:
            post("dmitro", f"/razbor/sales/{s.id}", {"action": "sold", "pay_method": rnd.choice(("cash", "card"))}, what="Получено, продано")


KIOSK = None


def attendance(day):
    """Каждый пришёл и ушёл по карте (проходная — киоск у входа)."""
    global KIOSK
    if KIOSK is None:
        KIOSK = P("admin")
    for u in q(M.User, M.User.active.is_(True), M.User.login.notin_(("admin", "director", "deputy"))):
        if (day.weekday() >= 5 and day != TODAY) or rnd.random() < 0.04:   # 22.11.2026: сб и вс — выходные
            continue
        card = one(M.StaffCard, M.StaffCard.user_id == u.id)
        if not card:
            continue
        # 25.11.2026 (брат): день 8:00–19:00. Чаще вовремя, изредка опоздание / ранний уход / забыл отметить уход
        # (менеджер напомнит). Прошлые дни — отметка картой прямо в базу: проходная решает «пришёл / ушёл» по
        # настоящей сегодняшней дате, и при «перемотке» времени все прошлые отметки были бы «пришёл».
        t_in = (f"08:{rnd.randint(16, 40):02d}" if rnd.random() < 0.06 else
                f"0{rnd.choice((7, 7, 8))}:{rnd.randint(35, 59) if rnd.random() < 0.7 else rnd.randint(0, 10):02d}")
        t_out = (f"18:{rnd.randint(20, 40):02d}" if rnd.random() < 0.04 else f"19:{rnd.randint(0, 25):02d}")
        if t_in.startswith("08:") and int(t_in[3:]) > 10 and int(t_in[3:]) < 16:
            t_in = "07:58"
        for hhmm, d in ((t_in, "in"), (t_out, "out")):
            if d == "out" and (day == TODAY or rnd.random() < 0.04):
                continue   # изредка забыли отметить уход; сегодня — ещё на работе
            if day < TODAY:
                hh_, mm_ = map(int, hhmm.split(":"))
                with SessionLocal() as db_:
                    db_.add(M.AttendanceEvent(user_id=u.id, branch_id=u.branch_id, card_id=card.id, direction=d, method="card",
                                              at=datetime.combine(day, time(hh_, mm_), tzinfo=LOCAL).astimezone(timezone.utc)))
                    db_.commit()
                continue
            with at(day, hhmm):
                r = KIOSK.post("/kiosk/tap", data={"uid": card.uid})
                if r.status_code != 200:
                    R.fail("проходная", f"карта {u.full_name}", "/kiosk/tap", f"ответ {r.status_code}: {r.text[:120]}")


def payroll_week(monday):
    """Понедельник: неделя до него. Админ — оклад по табелю; бухгалтер проверил; админ утвердил; бухгалтер выдал."""
    prev = monday - timedelta(days=7)
    d = str(prev)
    with at(monday, "09:30"):
        post("admin", f"/payroll/weeks/{d}/salary", what="Начислить оклад за неделю")
        if rnd.random() < 0.5:
            post("admin", f"/payroll/weeks/{d}/adjust", {"user_id": str(uid("andrey")), "amount": "500", "kind": "bonus",
                                                         "note": "За срочную машину в субботу"}, what="Премия")
    # 25.11.2026 (брат): сначала руководство решает по табелю прошлой недели (опоздания, неявки, ранний уход, задержка) —
    # без решения бухгалтер неделю сотрудника «Проверено» не отметит. О директоре — админ, о заме — директор.
    with at(monday, "09:45"):
        from app.services import lateness as _ls
        with SessionLocal() as db:
            br = db.scalar(select(M.Branch))
            dr = db.scalar(select(M.User).where(M.User.login == "director"))
            todo = [(x["f"].user.id, x["f"].day, x["f"].kind, x["can"]) for x in _ls.for_boss(db, br.id, dr)
                    if prev <= x["f"].day < monday and x["f"].kind != "no_out"]
        for u_id, d_, kind, can in todo:
            who = "director" if can else "admin"
            if kind == "overtime":
                post(who, "/timesheet/decide", {"user_id": str(u_id), "day": d_.isoformat(), "kind": kind, "decision": "nobonus"},
                     what="Табель: задержка — без премии")
            else:
                v = rnd.choice(("deduct", "deduct", "excused", "leave"))
                post(who, "/timesheet/decide", {"user_id": str(u_id), "day": d_.isoformat(), "kind": kind, "decision": v,
                                                "reason": "car", "comment": "Пробка из-за ДТП"},
                     what="Табель: решение по " + {"late": "опозданию", "absent": "неявке", "early": "раннему уходу"}.get(kind, kind))
    with at(monday, "10:00"):
        from app.services import payweek as wsvc
        from app.services.staff import staff_for_period
        with SessionLocal() as db:
            br = db.scalar(select(M.Branch))
            data = wsvc.week_data(db, br.id, prev, staff_for_period(db, br.id, prev))
        # 12.11.2026: неделя пройдена не до конца — у бухгалтера и директора остаётся работа на сегодня:
        # двое не проверены, двое проверены, но не утверждены, остальные утверждены (выдана часть)
        act = [(u_id, r) for u_id, r in data["rows"].items() if r["accrued"] > 0]
        # 26.11.2026 (брат): старые недели проходят до конца (проверил → утвердил → выдал); незаконченная — только прошлая
        last = monday == TODAY - timedelta(days=TODAY.weekday())
        for i, (u_id, r) in enumerate(act):
            if i < 2 and last:
                continue   # ждут проверки бухгалтера
            if r["pst"] == "open":
                post("buh", f"/payroll/weeks/{d}/people/{u_id}", {"status": "checked"}, what="Проверил")
            if i >= 4 or not last:   # 20.11.2026 (брат): утверждает директор (админ дал ему «Зарплату сотрудников»)
                post("director", f"/payroll/weeks/{d}/people/{u_id}", {"status": "approved"}, what="Утвердил (директор)")
    with at(monday + timedelta(days=1), "17:00"):
        with SessionLocal() as db:
            br = db.scalar(select(M.Branch))
            data = wsvc.week_data(db, br.id, prev, staff_for_period(db, br.id, prev))
        appr = [(u_id, r) for u_id, r in data["rows"].items() if r["pst"] == "approved" and r["due"] > 0]
        for u_id, r in (appr[: max(1, len(appr) - 2)] if last else appr):   # двоим ещё не выдали (прошлая неделя)
            post("buh", f"/payroll/weeks/{d}/pay", {"user_id": str(u_id)}, what="Выдал зарплату")
        with SessionLocal() as db:
            data = wsvc.week_data(db, br.id, prev, staff_for_period(db, br.id, prev))
        for u_id, r in data["rows"].items():
            if r["pst"] == "paid":
                R.check(r["due"] <= 0, "после выдачи за неделю долг сотруднику 0", f"{prev}: {u_id} осталось {r['due']}")


def schedule_tomorrow(day):
    """Менеджер записывает клиентов на завтра."""
    out = []
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5 and nxt != TODAY:   # 22.11.2026: сб и вс — выходные, записываем на понедельник
        nxt += timedelta(days=1)
    if nxt >= TODAY + timedelta(days=3):
        return out
    # 20.11.2026: машин мало — по записи приезжает одна в день (через раз); на будущие дни — две (это ещё не машины)
    for _ in range(rnd.choice((0, 1)) if nxt <= TODAY else 2):
        c = free_client(repeat=True)
        mgr = rnd.choice(MANAGERS)
        mech = rnd.choice(["andrey", "andrey", "elektro", "motor"])
        hh = rnd.randint(9, 15)
        with at(day, f"{rnd.randint(14, 17)}:{rnd.randint(0, 59):02d}"):
            post(mgr, "/schedule", {"day": str(nxt), "time": f"{hh:02d}:00", "duration": "60", "mechanic_id": str(uid(mech)),
                                    "client_name": c["name"], "phone": c["phone"], "car": f"{c['make']} {c['model']}",
                                    "plate": c["plate"], "work": "ТО", "note": "", "view": "day"}, what="Записать клиента на завтра")
        from app.services.orders import norm_phone
        a = q(M.Appointment, M.Appointment.status == "booked", M.Appointment.phone == norm_phone(c["phone"]),
              order=M.Appointment.created_at.desc())
        if a:
            out.append({"id": str(a[0].id), "client": c, "mgr": mgr, "hh": hh, "day": nxt})
    return out


ROMAN_GONE = [False]


def dismiss_roman(day):
    """Конец дня: директор увольняет Романа — его открытые наряды передаёт Андрею (без передачи CRM не даст)."""
    rid = uid("roman")
    with at(day, "18:30"):
        post("director", f"/settings/users/{rid}/dismiss", {"reason": "проверка: без передачи нарядов"}, what="Уволить без передачи (должно не дать)")
        R.check(one(M.User, M.User.id == rid).active or not q(M.WorkOrder, M.WorkOrder.mechanic_id == rid, M.WorkOrder.status.notin_(("closed", "cancel"))),
                "без передачи нарядов уволить нельзя", "")
        data = {"dismissed_on": str(day), "reason": "По собственному желанию"}
        from app.services.staff import open_orders
        with SessionLocal() as db:
            for o, slots in open_orders(db, db.get(M.User, rid)):
                for s in slots:
                    data[f"wo_{o.id}_{s}"] = str(uid("andrey"))
        post("director", f"/settings/users/{rid}/dismiss", data, what="Уволить Романа с передачей нарядов")
    R.check(not one(M.User, M.User.id == rid).active, "Роман уволен", "")
    R.check(not q(M.WorkOrder, M.WorkOrder.mechanic_id == rid, M.WorkOrder.status.notin_(("closed", "cancel"))),
            "открытые наряды уволенного переданы", "")
    for o in ACTIVE:
        if o.lead == "roman":
            o.lead = "andrey"
    ROMAN_GONE[0] = True


# ================================================================ главный цикл
STAFF = ("admin", "irina", "oleg", "pavel", "dmitro", "andrey", "director", "deputy", "buh",
         "elektro", "motor", "moyka", "detail", "glav")   # 20.11.2026: + главный кладовщик


def prepare_staff():
    """12.11.2026: тестовые входы при первом запуске — только нужный состав и с настоящими именами
    (второго слесаря нет). Меняется только список для этой базы, app/seed.py не трогаем."""
    from app import seed as sd
    sd.TEST_USERS[:] = [(lg, NAMES.get(lg, nm), *rest) for lg, nm, *rest in sd.TEST_USERS if lg in STAFF]


def end_of_day(day):
    """Вечер: каждый открыл «Сообщения» — прочитанное за день не висит окошками на следующий."""
    with at(day, "19:20"):
        for lg in STAFF + tuple(s[0] for s in STAFF_EXTRA):
            get(lg, "/notifications")


def remind_appts(day):
    """Записи под «Напомнить о записи» (20.11.2026, брат): сегодня 09:00 / 10:30 / 12:00 — не приехали (🔴),
    завтра 4 записи (🟠); одному из завтрашних позвонили — подтвердил, одну отменили (клиент не приедет)."""
    # время — вне 09–15 (там записи schedule_tomorrow), чтобы не было пересечений у мастера
    plan = [(day, "08:00", "andrey", "ТО, масло и фильтры"), (day, "16:00", "elektro", "Не заводится, проверить АКБ"),
            (day, "17:00", "andrey", "Стук спереди справа"),
            (day + timedelta(days=1), "08:00", "andrey", "Замена колодок"), (day + timedelta(days=1), "16:00", "motor", "Течь масла"),
            (day + timedelta(days=1), "17:00", "elektro", "Горит Check Engine"), (day + timedelta(days=1), "18:00", "andrey", "Развал-схождение")]
    made = []
    from app.services.orders import norm_phone
    used = set()
    for i, (d, hh, mech, work) in enumerate(plan):
        # клиент без машины у нас сейчас (иначе «не приехал» про машину, которая стоит в ремонте) и без повторов
        busy = {o.c["plate"] for o in ACTIVE if o.wo() is not None and o.wo().status not in ("closed", "cancelled")}
        pool = [x for x in CLIENTS if x["plate"] not in busy and x["plate"] not in used] or CLIENTS
        c = rnd.choice(pool)
        used.add(c["plate"])
        with at(day - timedelta(days=1) if d == day else day, f"{15 + i % 3}:{10 * (i % 5):02d}"):
            post("irina", "/schedule", {"day": str(d), "time": hh, "duration": "60", "mechanic_id": str(uid(mech)),
                                        "client_name": c["name"], "phone": c["phone"], "car": f"{c['make']} {c['model']}",
                                        "plate": c["plate"], "work": work, "note": "", "view": "day"}, what="Запись (напомнить)")
        a = q(M.Appointment, M.Appointment.phone == norm_phone(c["phone"]), M.Appointment.status == "booked",
              order=M.Appointment.created_at.desc())
        if a:
            made.append(a[0])
    tmr = [a for a in made if a.start_at.astimezone(LOCAL).date() > day]
    if len(tmr) >= 3:
        with at(day, "17:30"):
            post("irina", f"/schedule/{tmr[0].id}/called", what="Напомнить: позвонил — подтвердил")
            post("irina", f"/schedule/{tmr[1].id}/status", {"status": "cancel", "back": "/schedule/remind"},
                 what="Напомнить: клиент не приедет — отменить")
    from app.services import schedule as sch
    with SessionLocal() as db:
        r = sch.reminders(db, one(M.Branch).id)
    R.check(len(r["call"]) >= 1, "«Напомнить о записи»: завтрашние — позвонить", str(len(r["call"])))


def leave_for_today(day):
    """Сегодня у каждого — своё незаконченное дело, чтобы зайти под ним и пройти путь дальше руками."""
    # Мастерам — свои машины: электрику и мотористу — на диагностику, детейлеру — сразу в работу
    for kind, hh in (("electrician", "15:10"), ("motorist", "15:30")):
        new_order(day, "irina", hh, kind=kind, scen="B")
    o = new_order(day, "irina", "15:50", kind="detailer", scen="A")
    if o:
        step_price(o, day, "16:00")
        step_pick(o, day, "16:10")
        step_approve(o, day, "16:15")
    # 13.11.2026: новый порядок «подбор ↔ согласование» — отдельные машины, чтобы каждая кнопка была на сегодня.
    def fresh_mech(hh):
        o_ = new_order(day, "irina", hh, kind="mechanic", scen="B")
        if o_:
            o_.fail_plan = None   # обычная машина: без «отказался от всего» и отмены
            step_diag(o_, day, _later(hh, 5))
            step_price(o_, day, _later(hh, 60))
        return o_

    def option_pair(o_, hh, name, pair):
        with at(day, hh):
            g = ""
            for brand, cost in pair:
                post("oleg", f"{o_.url}/parts", {"name": name, "brand": brand, "qty": "2", "cost": cost, "markup_pct": "35",
                                                 "action": "option", "option_group": g}, what="Вариант на выбор клиенту")
                g = g or (one(M.WoPart, M.WoPart.work_order_id == o_.id, M.WoPart.option_group.is_not(None)) or M.WoPart()).option_group or ""

    # 0) 23.11.2026 (брат): проблемы записаны — менеджеру 🔴 «впишите работы» (одна работа уже есть, «Работы записаны»
    #    не нажато), запчастисту 🟠 «скоро подбор»
    o = new_order(day, "irina", "15:40", kind="mechanic", scen="B")
    if o:
        o.fail_plan = None
        step_diag(o, day, "15:45")
        with at(day, "16:30"):
            j = o.jobs[0]
            post(o.mgr, f"{o.url}/works", {"name": j[1], "pricing": "fixed" if j[2] is None else "hours", "hours": str(j[2] or 0),
                                           "rate": str(j[3]), "qty": "1", "time_h": str(j[2] or 1)}, what="Работа (шаг «Работы»)")
        R.check(o.wo().works_sent_at is None, "на сегодня: менеджер пишет работы, подбор ещё закрыт", o.wo().number)
        o.step = "works_wait"
    # 1) Запчастисту: подбор ещё идёт — одна деталь добавлена, «Подбор завершён» не нажат (менеджер ждёт)
    o = fresh_mech("13:10")
    if o:
        with at(day, "14:30"):
            post("oleg", f"{o.url}/parts", {"name": "Сайлентблок заднего рычага", "brand": "Febi", "article": "27484",
                                            "qty": "2", "cost": "420", "markup_pct": "35", "action": "order"}, what="Заказать (подбор идёт)")
        o.step = "pick_wait"
    # 2) Менеджеру: подбор готов — варианты фирм на выбор, одна позиция уже в архиве (клиент сразу отказался)
    o = fresh_mech("13:40")
    if o:
        option_pair(o, "15:00", "Диск тормозной передний", (("Bosch", "880"), ("TRW", "1120")))
        with at(day, "15:05"):
            post("oleg", f"{o.url}/parts", {"name": "Датчик износа колодок", "brand": "Bosch", "article": "1 987 473 562",
                                            "qty": "1", "cost": "260", "markup_pct": "40", "action": "order"}, what="Заказать: датчик")
            post("oleg", f"{o.url}/parts", {"name": "Комплект направляющих суппорта", "brand": "Febi", "article": "38722",
                                            "qty": "1", "cost": "390", "markup_pct": "40", "action": "order"}, what="Заказать: направляющие")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        with at(day, "15:20"):
            pn = next((p for p in parts_of(o) if p.name == "Комплект направляющих суппорта"), None)
            if pn:
                post("irina", f"{o.url}/parts/{pn.id}/delete", what="Клиент отказался — в архив")
        o.step = "approve_wait"
    # 3) Запчастисту: менеджер согласовал — заказ по наряду (3 строки) ждёт «Заказал» весь или по строке
    o = fresh_mech("14:10")
    if o:
        with at(day, "15:30"):
            for name, art, brand, cost in (("Колодки тормозные задние (оригинал)", "04466-42060", "Toyota", "2350"),
                                           ("Диск тормозной задний", "DF4294", "TRW", "1380"),
                                           ("Тормозной шланг задний", "PHB572", "TRW", "540")):
                post("oleg", f"{o.url}/parts", {"name": name, "article": art, "brand": brand, "qty": "1", "cost": cost,
                                                "markup_pct": "30", "action": "order"}, what=f"Заказать: {name}")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        step_approve(o, day, "16:45", send=False)
        if o.step == "wait_parts":
            o.step = "draft"
    # 4) Слесарю: новая машина — диагностика («новые — посмотреть»)
    o = new_order(day, "irina", "16:05", kind="mechanic", scen="B")
    if o:
        o.fail_plan, o.step = None, "diag_wait"
    # 5) Слесарю: машина «В работе» — работы ещё не отмечены (Начал / Пауза / Выполнено), запчасти выданы
    o = fresh_mech("11:20")
    if o:
        with at(day, "12:30"):
            post("oleg", f"{o.url}/parts", {"catalog_id": str(cat_id(OIL) or ""), "name": OIL,
                                            "qty": "1", "cost": "", "markup_pct": "40", "action": "auto"}, what="Со склада")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        step_approve(o, day, "12:45")
        if o.step == "issue":
            step_issue(o, day, "13:00")
        o.step = "work_wait"
    # 5б) 20.11.2026: всё со склада, согласовано — запчасти «на складе», мастер ещё не запросил выдачу
    #     (ниже: одной машине «Запросить выдачу», другой — кладовщик «📦 Готово к выдаче» → менеджер «забрать у окошка»)
    for hh in ("09:40", "10:05"):
        o = fresh_mech(hh)
        if o:
            step_pick(o, day, _later(hh, 70))
            step_approve(o, day, _later(hh, 90))
            if o.wo().status == "instock":
                o.step = "instock_today"
    # 6) Всем по наряду: пришла часть заказа («Пришли запчасти», зелёная цифра) — остальное ещё едет
    o = fresh_mech("10:40")
    if o:
        with at(day, "11:50"):
            for name, art, brand, cost in (("Рычаг передний левый", "TC1310", "Lemförder", "2650"),
                                           ("Шаровая опора", "JBJ689", "TRW", "780")):
                post("oleg", f"{o.url}/parts", {"name": name, "article": art, "brand": brand, "qty": "1", "cost": cost,
                                                "markup_pct": "35", "action": "order"}, what=f"Заказать: {name}")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        step_approve(o, day, "12:10", send=True)
        lines = [l for p in q(M.Purchase, M.Purchase.work_order_id == o.id) for l in q(M.PurchaseLine, M.PurchaseLine.purchase_id == p.id)]
        if lines:
            with at(day, "17:00"):
                pid = str(lines[0].purchase_id)
                loc = (q(M.Location, M.Location.rack == "B") or q(M.Location))[0]
                post("pavel", "/stock/receive", {"line_id": str(lines[0].id), f"loc_{lines[0].id}": str(loc.id), f"inv_{lines[0].id}": "ТТН-55120"},
                     what="Принять одну строку — пришла часть")
            # 19.11.2026 (брат): слесарь берёт то, что пришло, пока остальное едет (выдать — кладовщик руками)
            with at(day, "17:05"):
                post(o.lead, f"{o.url}/issue-request", {"comment": "начну с того, что пришло"},
                     what="Запросить выдачу пришедшего (остальное едет)")
            R.check(one(M.IssueRequest, M.IssueRequest.work_order_id == o.id, M.IssueRequest.status == "new") is not None
                    and o.wo().status == "waiting", "часть пришла — слесарь запросил выдачу, наряд «Ждём запчасти»",
                    o.wo().number)
        o.step = "wait_parts_today"
    # 2б) 19.11.2026 (брат): подбор готов — колодки TRW со склада и Jurid под заказ ОДНОЙ группой (клиент выбирает одно);
    #     у менеджера — «Выбрать», затем «Согласовал с клиентом»
    o = fresh_mech("13:55")
    pads = cat_id(PADS)
    if o and pads:
        with at(day, "15:10"):
            post("oleg", f"{o.url}/parts", {"catalog_id": str(pads), "name": PADS, "qty": "1",
                                            "cost": "", "markup_pct": "40", "action": "auto"}, what="Со склада: колодки")
            base = next((p for p in parts_of(o) if p.status == "reserved"), None)
            if base:
                post("oleg", f"{o.url}/parts", {"name": PADS, "brand": "Jurid", "qty": "1",
                                                "cost": "1050", "markup_pct": "35", "action": "option",
                                                "option_group": f"part:{base.id}"}, what="Вариант к колодкам со склада")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        R.check(len({p.option_group for p in parts_of(o) if p.status == "option"}) == 1 and o.wo().picked_at,
                "колодки со склада и под заказ — одна группа на выбор клиенту", o.wo().number)
        o.step = "approve_wait"
    # 2в) 19.11.2026: одна деталь дважды (масло под две работы) — у менеджера предупреждение «клиент берёт все?»
    o = fresh_mech("14:25")
    oil = cat_id(OIL)
    if o and oil:
        with at(day, "15:15"):
            for _ in range(2):
                post("oleg", f"{o.url}/parts", {"catalog_id": str(oil), "name": OIL, "qty": "1",
                                                "cost": "", "markup_pct": "40", "action": "auto"}, what="Со склада: масло")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        o.step = "approve_wait"
    # 6б) 19.11.2026 (брат): клиент отказался ПОСЛЕ согласования. Заказано у поставщика: одна деталь — ждёт решения
    #     запчастиста (🔴), вторая — «пусть приедет на склад», приехала (🟠 оставить / вернуть / продать)
    o = fresh_mech("10:20")
    if o:
        with at(day, "11:30"):
            for name, art, brand, cost in (("Амортизатор задний", "343457", "Kayaba", "1900"),
                                           ("Опора амортизатора", "MK018", "SACHS", "640"),
                                           ("Отбойник амортизатора", "900046", "SACHS", "310")):
                post("oleg", f"{o.url}/parts", {"name": name, "article": art, "brand": brand, "qty": "2", "cost": cost,
                                                "markup_pct": "35", "action": "order"}, what=f"Заказать: {name}")
            post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
        step_approve(o, day, "11:50", send=True)
        ps = {p.name: p for p in parts_of(o)}
        with at(day, "13:10"):
            for nm in ("Опора амортизатора", "Отбойник амортизатора"):
                if nm in ps:
                    post(o.mgr, f"{o.url}/parts/{ps[nm].id}/delete", {"reason": "Клиент поставит свои — купил на разборке"},
                         what="Клиент отказался (уже заказано)")
        lo = one(M.PartLeftover, M.PartLeftover.wo_part_id == ps["Отбойник амортизатора"].id) if "Отбойник амортизатора" in ps else None
        R.check(lo is not None and lo.status == "decide", "отказ от заказанного — решает запчастист", o.wo().number)
        if lo:
            with at(day, "14:00"):
                post("oleg", f"/stock/leftovers/{lo.id}", {"action": "to_stock", "note": "Поставщик не принимает отказ"},
                     what="Отказ клиента: пусть приедет на склад")
            line = one(M.PurchaseLine, M.PurchaseLine.id == lo.purchase_line_id)
            loc = (q(M.Location, M.Location.rack == "B") or q(M.Location))[0]
            if line:
                with at(day, "16:40"):
                    post("pavel", "/stock/receive", {"line_id": str(line.id), f"loc_{line.id}": str(loc.id),
                                                     f"inv_{line.id}": "ТТН-61205"}, what="Приняли отказанное — на склад")
            get("oleg", "/stock/leftovers", what="Лишнее после отказов")
            R.check(one(M.PartLeftover, M.PartLeftover.id == lo.id).status == "stock",
                    "отказанное приехало — «на складе, решить»", o.wo().number)
        o.step = "refused_today"
    # 7) 13.11.2026: машина на диагностике у двух мастеров — слесарь закончил, электрик ещё нет
    c = free_client()
    with at(day, "13:30"):
        r = post("irina", "/orders/new", {"phone": c["phone"], "name": c["name"], "plate": c["plate"], "make": c["make"],
                                          "model": c["model"], "year": str(c["year"]), "mileage": str(c["km"] + 3000),
                                          "vin": c["vin"], "scenario": "B", "complaint": "Не заводится с утра, стук спереди",
                                          "partsman_id": str(uid("oleg")), "mechanic_id": str(uid("andrey")), "lift_id": "",
                                          "fuel_level": "50", "intake_items": ["docs"], "intake_token": f"sim-{rnd.random()}",
                                          "extra_master_id": str(uid("elektro")), "extra_work": "Диагностика электрики"},
                 files=photo(1), what="Приёмка: слесарь + электрик", fetch=True)
    if r is not None and r.status_code == 200:
        url = r.json()["url"]
        with at(day, "14:20"):
            post("andrey", f"{url}/findings", {"text": "Стук правой стойки стабилизатора", "urgency": "soon"},
                 files=photo(1), what="Находка слесаря", fetch=True)
            post("andrey", f"{url}/diag-done", what="Моя диагностика готова (слесарь)")
        R.check(one(M.WorkOrder, M.WorkOrder.id == uuid.UUID(url.rsplit("/", 1)[1])).status == "diag",
                "два мастера: пока электрик не закончил — наряд на диагностике", url)
    # 8) Передача: машину переставили на пост электрика — ему «Мне передали», ждёт «✓ Принял»
    o = next((o for o in ACTIVE if o.wo() and o.wo().status in ("instock", "waiting", "approve")
              and o.wo().mechanic_id == uid("andrey") and o.kind == "mechanic"), None)
    if o:
        wo = o.wo()
        with at(day, "16:50"):   # другой мастер; его пост занят — машина встаёт к нему в очередь
            post("irina", f"{o.url}/assign", {"partsman_id": str(wo.partsman_id or ""), "mechanic_id": str(uid("motor")),
                                              "lift_id": str(wo.lift_id or ""), "manager_id": str(wo.manager_id)},
                 what="Передать: другому мастеру")
        R.check(o.wo().mechanic_id == uid("motor"), "передача мастеру: машина у нового мастера", o.wo().number)
        o.step, o.lead = "handed_wait", "motor"
    # 9) Ещё мастер в наряде «В работе» — мойщик (ему «Мне передали»)
    o = next((o for o in ACTIVE if o.wo() and o.wo().status == "work"), None)
    if o:
        with at(day, "17:35"):
            post("irina", f"{o.url}/masters", {"user_id": str(uid("moyka")), "work": "Мойка кузова после ремонта"},
                 what="Добавить мастера: мойщик")
    # 10) 19.11.2026 — одна очередь мойки на сегодня (руками: менеджер, мойщик, детейлер)
    wash_today(day, skip={o.id} if o else set())
    # Кладовщик: накладная ждёт — принять на склад руками
    p = next(iter(q(M.Purchase, M.Purchase.status == "ordered", M.Purchase.kind != "chem")), None)
    if p is None:
        restock(day)
        p = next(iter(q(M.Purchase, M.Purchase.status == "ordered", M.Purchase.kind != "chem")), None)
    # 14.11.2026: шага «Привезли» нет — накладная просто ждёт в «Ждём», принять — руками
    # Мастер просит выдать, кладовщик ещё не выдал; второй наряд — «Готово к выдаче», менеджер не забрал
    issue = [o for o in ACTIVE if o.wo() and o.wo().status == "instock"]
    for i, o in enumerate(issue[:2]):
        with at(day, "17:10"):
            post(o.lead, f"{o.url}/issue-request", {"comment": "к 18:00"}, what="Запросить выдачу (выдать — руками)")
            r = one(M.IssueRequest, M.IssueRequest.work_order_id == o.id, M.IssueRequest.status == "new")
            if i == 1 and r:
                post("dmitro" if r.zone == "razbor" else "pavel", f"/stock/issues/{r.id}/ready",
                     what="📦 Готово к выдаче (забрать — руками)")
    # 20.11.2026: общий инструмент на руках у мастеров («Выдан под наряды» не пустой); один ещё не подтверждён
    held = [o for o in ACTIVE if o.wo() and o.wo().status in ("diag", "approve", "picking", "waiting", "instock", "work")]
    free_t = [t for t in q(M.Tool, M.Tool.category.notin_(("personal", "spare"))) if not t.holder_id and t.status in ("ok", "worn")]
    for k, (o, t) in enumerate(zip(_one_per_lead(held), free_t[2:5])):
        with at(day, f"16:{10 + k * 5}"):
            post(o.lead, f"{o.url}/tool-requests", {"tool_id": str(t.id), "comment": "под эту машину"}, what="Попросить инструмент")
            r = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
            if r:
                post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать инструмент (на руках)")
                if k:   # первый — «✓ Получил» ещё не нажат (зелёная цифра у мастера)
                    post(o.lead, f"/tool-requests/{r.id}/received", what="✓ Получил инструмент")
    # 20.11.2026: второй способ возврата — принесли к окошку без «Сдаю», кладовщик в «Выдан под наряды» жмёт «Вернул»
    extra = [t for t in q(M.Tool, M.Tool.category.notin_(("personal", "spare"))) if not t.holder_id and t.status in ("ok", "worn")]
    tool_masters = [o for o in held if o.lead in MECHS + ["elektro", "motor"]]
    if tool_masters and extra:
        o, t = tool_masters[-1], extra[-1]
        with at(day, "16:40"):
            post(o.lead, f"{o.url}/tool-requests", {"tool_id": str(t.id)}, what="Попросить инструмент")
            r = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
            if r:
                post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать инструмент")
                post(o.lead, f"/tool-requests/{r.id}/received", what="✓ Получил инструмент")
        with at(day, "16:55"):
            post("pavel", f"/tools/{t.id}/returned", what="Принесли к окошку — «Вернул»")
            R.check(one(M.Tool, M.Tool.id == t.id).holder_id is None, "«Вернул» — инструмент снова в шкафу", t.name)
    # Слесарь просит инструмент из шкафа; мойщик — химию; слесарь — запчасть со склада под наряд (согласует менеджер)
    work = sorted([o for o in ACTIVE if o.wo() and o.wo().status in ("work", "instock") and o.lead == "andrey"],
                  key=lambda o: o.wo().status != "work")   # «попросить под наряд» — только машина в ремонте
    if work:
        o = work[0]
        tools = [t for t in q(M.Tool, M.Tool.category.notin_(("personal", "spare"))) if not t.holder_id]
        if tools:
            with at(day, "17:15"):
                post(o.lead, f"{o.url}/tool-requests", {"tool_id": str(tools[0].id), "comment": "под эту машину"},
                     what="Попросить инструмент (выдать — руками)")
            if len(tools) > 1:   # 19.11.2026: второй — кладовщик уже подготовил («готово — забрать», зелёная у слесаря)
                with at(day, "17:18"):
                    post(o.lead, f"{o.url}/tool-requests", {"tool_id": str(tools[1].id), "comment": "ещё нужен"},
                         what="Попросить ещё инструмент")
                    r = one(M.ToolRequest, M.ToolRequest.tool_id == tools[1].id, M.ToolRequest.status == "new")
                    if r:
                        post("pavel", f"/tool-requests/{r.id}/ready", what="Инструмент: готово к выдаче (забрать — руками)")
        it = next((i for i in q(M.StockItem) if i.catalog_id == cat_id(CLEANER)), None)
        if it:
            with at(day, "17:20"):
                post("andrey", "/messages/ask", {"item_id": str(it.id), "order_id": str(o.id), "qty": "1",
                                                 "text": "Нужен очиститель тормозов, свой закончился"}, what="Попросить под наряд")
        # 20.11.2026 (брат): б/у с разбора под наряд — слесарь просит, менеджер согласует → запрос в зону «разбор»
        # (выдаёт кладовщик разбора; у обычного кладовщика — для сведения)
        from app.services import stock as _ss
        with SessionLocal() as _db:
            used = [i for i in _db.scalars(select(M.StockItem).join(M.PartCatalog, M.PartCatalog.id == M.StockItem.catalog_id)
                                            .where(M.PartCatalog.kind == "used", M.StockItem.price_state == ""))]
            bal = _ss.balances(_db, used[0].branch_id, [i.id for i in used]) if used else {}
            used = [i for i in used if bal[i.id]["free"] > 0]
        if used and o.wo().status == "work":
            with at(day, "17:22"):
                post("andrey", "/messages/ask", {"item_id": str(used[0].id), "order_id": str(o.id), "qty": "1",
                                                 "text": "Есть б/у на разборе — подойдёт"}, what="Попросить б/у с разбора")
                m = one(M.OrderMessage, M.OrderMessage.work_order_id == o.id, M.OrderMessage.status == "pending",
                        M.OrderMessage.stock_item_id == used[0].id)
                if m:
                    post(o.mgr, f"/messages/{m.id}/decide", {"action": "approve", "back": "/ready"}, what="Согласовать б/у")
            rz = one(M.IssueRequest, M.IssueRequest.work_order_id == o.id, M.IssueRequest.zone == "razbor",
                     M.IssueRequest.status == "new")
            R.check(rz is not None, "б/у под наряд — отдельный запрос кладовщику разбора", o.wo().number)
        with at(day, "17:25"):
            post("andrey", "/messages", {"order_id": str(o.id), "to": "manager",
                                         "text": "Клиенту стоит поменять задние колодки — осталось ~3 мм", "back": o.url},
                 what="Сообщение менеджеру")
    with at(day, "17:30"):
        chem_ask("moyka")
        chem_ask("detail")
    # 19.11.2026: одну химию мойщику кладовщик подготовил (зелёная «готово — забрать»), детейлеру — ещё ждёт (оранжевая)
    with at(day, "17:35"):
        chem_ask("moyka")
    r = one(M.ChemRequest, M.ChemRequest.status == "new", M.ChemRequest.created_by == uid("moyka"))
    if r and free_qty(r.catalog_id) >= r.qty:
        with at(day, "17:40"):
            post("pavel", f"/chem/{r.id}/ready", {"qty": str(r.qty)}, what="Химия: готово к выдаче (забрать — руками)")
    # 18.11.2026 (брат): кладовщику — «Всегда в наличии»: одну позицию дозаказал, но «Заказан» ещё не нажал (черновик
    # «Заказ на склад» ждёт поставщика), а вторая — ниже нормы и не заказана (красная строка, сиреневая цифра)
    keep_names = [n for n, *_ in STOCK[3:10] if cat_id(n)]
    if len(keep_names) >= 2:
        with at(day, "17:50"):
            a, b = keep_names[0], keep_names[1]
            post("pavel", "/stock/always", {"catalog_id": str(cat_id(a)), "min_qty": str(int(free_qty(cat_id(a))) + 6)},
                 what="Всегда в наличии: норма выше остатка")
            post("pavel", "/stock/always/order/missing", what="Заказать недостающее (черновик — «Заказан» руками)")
            post("pavel", "/stock/always", {"catalog_id": str(cat_id(b)), "min_qty": str(int(free_qty(cat_id(b))) + 4)},
                 what="Всегда в наличии: ещё одна ниже нормы")
        r = get("pavel", "/api/notify", what="Цифры меню")
        R.check(r is not None and any(x["c"] == "purple" for x in r.json().get("badges", {}).get("/stock", [])),
                "сиреневая цифра «Всегда в наличии» у кладовщика")
    # Неполная поставка на сегодня: заказ кладовщика на склад — привезли часть одной позиции, остальное ждём
    sp = next((p for p in q(M.Purchase, M.Purchase.status == "ordered", M.Purchase.created_by == uid("pavel"))), None)
    sl = next((l for l in q(M.PurchaseLine, M.PurchaseLine.purchase_id == sp.id) if l.qty >= 2), None) if sp else None
    if sl:
        with at(day, "17:55"):
            post("pavel", "/stock/receive", {"line_id": str(sl.id), f"qty_{sl.id}": "1", f"inv_{sl.id}": "ТТН-30117"},
                 what="Неполная поставка: привезли 1 (остальное ждём)")
        R.check(one(M.Purchase, M.Purchase.id == sp.id).status == "partial", "заказ на склад — неполная поставка", str(sp.id))
    # 20.11.2026 (брат): «Напомнить о записи» — записанные на сегодня (время прошло, не приехали — 🔴) и на завтра
    # (позвонить сегодня — 🟠); одному уже позвонили (подтвердил), одну завтрашнюю отменили после звонка
    remind_appts(day)
    # Разбор: подготовить, упаковано, отправлено; новая машина — цены ждут директора, отказ, запрос изменения цены
    for stop in ("reserve", "reserve", "pack", "ship"):
        razbor_sale(day, stop=stop)
    razbor_today(day)
    # Директору и заму: открыть закрытый наряд, гарантия, доступ ко всем нарядам, доступ к клиенту
    # недавно выданные (менеджер видит выданное за 3 дня), сначала самые свежие
    closed = sorted([o for o in DONE if o.wo() and o.wo().status == "closed" and o.wo().closed_at
                     and o.wo().manager_id == uid("irina")   # 25.11.2026: просит менеджер наряда (чужой — 403)
                     and (TODAY - o.wo().closed_at.astimezone(LOCAL).date()).days <= 1],
                    key=lambda o: o.wo().closed_at, reverse=True)
    if len(closed) >= 2:
        with at(day, "17:40"):
            post("irina", f"{closed[0].url}/reopen-request", {"reason": "Забыли поменять салонный фильтр — клиент вернулся"},
                 what="Запрос «Открыть заново»")
            post("irina", f"{closed[1].url}/warranty-request", {"reason": "Через 3 дня снова скрипят передние колодки"},
                 what="Запрос «Ремонт по гарантии»")
    with at(day, "17:45"):
        post("irina", "/orders/access-request", {"reason": "Клиент спрашивает про прошлый ремонт у другого менеджера"},
             what="Доступ ко всем нарядам")
        old = [o for o in DONE if o.wo() and o.wo().closed_at and o.wo().status == "closed"
               and (TODAY - o.wo().closed_at.astimezone(LOCAL).date()).days >= 4]
        if old:
            post("irina", "/clients/access-request", {"reason": f"Звонит {old[0].c['name']} ({old[0].c['plate']}) — уточнить прошлый ремонт"},
                 what="Доступ к клиенту")


# ================================================================ 22.11.2026 (брат): новое для ручной проверки
SITE_DONE = [False]
SITE_PARTS = (("Рычаг передний правый", "TC1311", "Lemförder", "2650"), ("Пыльник ШРУСа наружный", "800 019", "Febi", "420"),
              ("Стойка стабилизатора задняя", "FE30442", "Febi", "560"), ("Подшипник ступицы передний", "VKBA 3644", "SKF", "1450"))


def site_history(day):
    """«На сайт»: клиент отказался от четырёх заказанных деталей → приехали на склад → запчастист «Продать» →
    директор утвердил три (одну — с фото и описанием на сайте; вторую продали через сайт с отправкой; третью запчастист
    взял в другой наряд — «нет на складе»), четвёртая ждёт утверждения."""
    SITE_DONE[0] = True
    o = new_order(day, "irina", "09:10", kind="mechanic", scen="B")
    if not o:
        return
    o.fail_plan = None
    step_diag(o, day, "09:15")
    step_price(o, day, "10:10")
    with at(day, "10:40"):
        for name, art, brand, cost in SITE_PARTS:
            post("oleg", f"{o.url}/parts", {"name": name, "article": art, "brand": brand, "qty": "1", "cost": cost,
                                            "markup_pct": "35", "action": "order"}, what=f"Заказать: {name}")
        post("oleg", f"{o.url}/picking-done", what="Подбор завершён")
    step_approve(o, day, "11:00", send=True)
    ps = {p.name: p for p in parts_of(o)}
    with at(day, "11:40"):
        for name, *_ in SITE_PARTS:
            if name in ps:
                post(o.mgr, f"{o.url}/parts/{ps[name].id}/delete", {"reason": "Клиент передумал — поставит позже сам"},
                     what="Клиент отказался (уже заказано)")
    los = [one(M.PartLeftover, M.PartLeftover.wo_part_id == ps[n].id) for n, *_ in SITE_PARTS if n in ps]
    los = [lo for lo in los if lo]
    loc = (q(M.Location, M.Location.rack == "B") or q(M.Location))[0]
    for lo in los:
        with at(day, "12:10"):
            post("oleg", f"/stock/leftovers/{lo.id}", {"action": "to_stock", "note": "Поставщик не принимает отказ"},
                 what="Отказ клиента: пусть приедет на склад")
        line = one(M.PurchaseLine, M.PurchaseLine.id == lo.purchase_line_id)
        if line and not line.received:
            with at(day, "14:20"):
                post("pavel", "/stock/receive", {"line_id": str(line.id), f"loc_{line.id}": str(loc.id),
                                                 f"inv_{line.id}": "ТТН-71840"}, what="Приняли отказанное — на склад")
        get("oleg", "/stock/leftovers", what="Лишнее после отказов")
        with at(day, "14:40"):
            post("oleg", f"/stock/leftovers/{lo.id}", {"action": "sale"}, what="Лишнее: «Продать» → на сайт")
    sn = {x.leftover_id: x for x in q(M.SiteNew)}
    items = [sn.get(lo.id) for lo in los]
    R.check(len([x for x in items if x]) == len(los) and all(x.state == "pending" for x in items if x),
            "«Продать» — деталь в «На сайт → Новые», ждёт утверждения", str(len(los)))
    items = [x for x in items if x]
    if len(items) < 4:
        return
    r1, r2, r3, _r4 = items
    with at(day, "15:30"):
        for x in (r1, r2, r3):
            post("director", f"/site/new/{x.id}/approve", what="На сайт: утвердить цену")
    with at(day, "16:10"):
        for x in (r1, r2):
            post("dmitro", f"/site/new/{x.id}", {"description": "Новая, в заводской упаковке. Осталась после отказа клиента.",
                                                 "publish": "1"}, files=photo(2), what="На сайт: фото и описание")
    with at(day, "16:40"):
        c = rnd.choice(CLIENTS)
        post("dmitro", f"/site/new/{r2.id}/sell", {"qty": "1", "buyer_name": "Покупатель с сайта", "buyer_phone": "0671239090",
                                                   "delivery": "np", "pay_method": "card", "channel": "site",
                                                   "np_address": "Киев, отделение 12", "action": "reserve"},
             what="На сайт: продать (резерв, отправка)")
    R.check(one(M.UsedSale, M.UsedSale.site_new_id == r2.id) is not None, "новая деталь продана через сайт — продажа разбора", r2.code)
    # 23.11.2026: подбор — только когда менеджер записал работы (шаг «Подбор»)
    other = next((x for x in ACTIVE if x is not o and x.wo() and x.wo().status == "approve" and x.wo().works_sent_at
                  and x.wo().partsman_id and not x.wo().picked_at), None)
    if other:
        with at(day, "17:10"):
            post("oleg", f"{other.url}/parts", {"catalog_id": str(r3.catalog_id), "name": SITE_PARTS[2][0], "qty": "1",
                                                "cost": "", "markup_pct": "35", "action": "auto"},
                 what="Подбор: со склада (деталь с сайта)")
        from app.services import site as site_svc
        with SessionLocal() as db:
            st_ = next((r["stage"] for r in site_svc.rows(db, r3.branch_id) if r["s"].id == r3.id), "")
        R.check(st_ == "nostock", "новую с сайта взяли в наряд — «нет на складе», с сайта ушла", st_)


def site_today(day):
    """Сегодня: кладовщик выставил из карточки склада «На продажу» две позиции; директор одну отклонил (у кладовщика
    разбора 🔴 «отказано»), вторая ждёт утверждения (у директора 🔴)."""
    for name, qty in (("Ролик натяжной", "1"), ("Лампа W5W (2 шт)", "2")):
        it = next((i for i in q(M.StockItem) if i.catalog_id == cat_id(name)), None)
        if it and free_qty(it.catalog_id) >= Decimal(qty):
            with at(day, "10:30"):
                post("pavel", f"/stock/items/{it.id}/to-site", {"qty": qty}, what=f"На продажу из склада: {name}")
    x = next((s_ for s_ in q(M.SiteNew, M.SiteNew.source == "manual") if s_.state == "pending"), None)
    if x:
        with at(day, "12:00"):
            post("director", f"/site/new/{x.id}/decline", {"answer": "Этот ролик нужен в ремонт — не продаём"},
                 what="На сайт: отказать с причиной")


def schedule_next_week(day):
    """Плотная запись на следующую неделю: у каждого мастера 1–3 записи в день по часам (свободные часы видно), одна
    запись в субботу «вышли», одну менеджер перенёс на другое время (клиент попросил)."""
    from app.services.orders import norm_phone
    mon = day + timedelta(days=(7 - day.weekday()) % 7 or 7)
    works = {"andrey": ["ТО, масло и фильтры", "Стук спереди", "Замена колодок", "Развал-схождение", "Замена ремня ГРМ"],
             "elektro": ["Не заводится", "Горит Check Engine", "Диагностика электрики"],
             "motor": ["Течь масла", "Троит двигатель", "Замена прокладки ГБЦ"],
             "moyka": ["Мойка кузова", "Комплексная мойка"], "detail": ["Полировка кузова", "Химчистка салона"]}
    made = []
    for k in range(5):
        d = mon + timedelta(days=k)
        for mech in works:
            free = list(range(9, 18))
            rnd.shuffle(free)
            taken = set()
            for _ in range(rnd.choice((1, 2, 2, 3))):
                dur = rnd.choice((60, 60, 120))
                hh = next((h for h in free if h not in taken and (dur == 60 or (h + 1 not in taken and h + 1 < 19))), None)
                if hh is None:
                    break
                taken.update({hh} if dur == 60 else {hh, hh + 1})
                c = rnd.choice(CLIENTS)
                with at(day, f"{rnd.randint(11, 16)}:{rnd.randint(0, 59):02d}"):
                    post("irina", "/schedule", {"day": str(d), "time": f"{hh:02d}:00", "duration": str(dur),
                                                "mechanic_id": str(uid(mech)), "client_name": c["name"], "phone": c["phone"],
                                                "car": f"{c['make']} {c['model']}", "plate": c["plate"],
                                                "work": rnd.choice(works[mech]), "note": "", "view": "week"},
                         what="Запись на следующую неделю")
                a = q(M.Appointment, M.Appointment.phone == norm_phone(c["phone"]), order=M.Appointment.created_at.desc())
                if a:
                    made.append((a[0], c, mech, dur, d))
    sat = mon + timedelta(days=5)
    c = rnd.choice(CLIENTS)
    with at(day, "16:30"):
        post("irina", "/schedule", {"day": str(sat), "time": "10:00", "duration": "120", "mechanic_id": str(uid("andrey")),
                                    "client_name": c["name"], "phone": c["phone"], "car": f"{c['make']} {c['model']}",
                                    "plate": c["plate"], "work": "Срочно: замена сцепления — вышли в субботу", "note": "",
                                    "view": "week"}, what="Запись в выходной «всё равно»")
    if made:
        a, c, mech, dur, d = made[0]
        with at(day, "17:20"):
            post("irina", f"/schedule/{a.id}", {"day": str(d), "time": "18:00", "duration": "60", "mechanic_id": str(uid(mech)),
                                                "lift_id": "", "client_name": c["name"], "phone": c["phone"],
                                                "car": f"{c['make']} {c['model']}", "plate": c["plate"], "work": a.work,
                                                "note": "Клиент попросил перенести на вечер", "view": "week"},
                 what="Перенести запись (позвонил клиенту)")
    R.check(len(made) >= 20, "плотная запись на следующую неделю", str(len(made)))


def queue_today(day):
    """Менеджер поднял наряд в очереди слесаря и электрика (↑ — раньше)."""
    from app.services import workload as wl
    from app.services.orders import staff_by_role
    for lg in ("andrey", "elektro"):
        with SessionLocal() as db:
            br = db.scalar(select(M.Branch))
            mid = uid(lg)
            qq = wl.queue_of(wl.plan(db, br.id, staff_by_role(db, br.id, "masters")), mid)
            solid = [b.wo.id for b in qq if b.kind == "solid"]
        if len(solid) >= 2:
            with at(day, "17:50"):
                post("irina", f"/schedule/queue/{solid[-1]}", {"mech": str(mid), "where": "up", "back": "/schedule"},
                     what="Очередь мастера: наряд раньше")
            with SessionLocal() as db:
                qq = wl.queue_of(wl.plan(db, br.id, staff_by_role(db, br.id, "masters")), mid)
                after = [b.wo.id for b in qq if b.kind == "solid"]
            R.check(after.index(solid[-1]) == len(solid) - 2, "↑ в очереди — наряд встал раньше", lg)


# ================================================================ 23.11.2026 (брат): каждая кнопка CRM
def _first(model, *where, order=None):
    r = q(model, *where, order=order)
    return r[0] if r else None


def cover_extras(day):
    """Кнопки, которые обычный ход недели не нажимает: настройки, сотрудники, клиенты, зарплата, склад, разбор,
    мойка, вход по PIN и карте, «глазами сотрудника». Каждая — там, где её нажал бы человек, и так, чтобы
    рабочая картина на сегодня осталась (что создали для проверки — то и убрали)."""
    from app.services.payweek import monday
    roles = {r.code: r.id for r in q(M.Role)}
    # ---- вход по PIN и по карте, выход, «глазами сотрудника»
    with at(day, "07:55"):
        c = TestClient(app)
        r = c.post("/login/pin", data={"user_id": str(uid("andrey")), "pin": "1005"}, follow_redirects=False)
        R.check(r.status_code == 303, "вход по PIN", str(r.status_code))
        c.post("/logout")
        card = _first(M.StaffCard, M.StaffCard.user_id == uid("pavel"))
        if card:
            r = TestClient(app).post("/login/card", data={"uid": card.uid}, follow_redirects=False)
            R.check(r.status_code in (200, 303), "вход по карте", str(r.status_code))
        post("admin", f"/view-as/{uid('andrey')}", what="Глазами сотрудника: Андрей")
        get("admin", "/orders", what="Наряды глазами Андрея")
        post("admin", "/view-as/stop", what="Выйти из «глазами сотрудника»")
        n = _first(M.Notification, M.Notification.user_id == uid("admin"))
        if n:
            post("admin", f"/api/notify/{n.id}/read", fetch=True, what="Окошко: прочитано")
    # ---- сотрудник-стажёр: завести, правка, пароль, карта, уволить, вернуть, снова уволить (будет в «Архиве»)
    with at(day, "08:10"):
        post("admin", "/settings/users", {"full_name": "Стажёр Тестовый", "login": "stazher", "role_id": str(roles["mechanic"]),
                                          "password": "stazher123", "pin": ""}, what="Новый сотрудник")
        st_ = _first(M.User, M.User.login == "stazher")
        if st_:
            post("admin", f"/settings/users/{st_.id}", {"full_name": "Стажёр Тестовый", "login": "stazher",
                                                       "role_id": str(roles["mechanic"]), "phone": "+380501112233",
                                                       "hired_on": str(day - timedelta(days=5))}, what="Сотрудник: правка")
            post("admin", f"/settings/users/{st_.id}/secret", {"password": "stazher123", "pin": "4321"}, what="Пароль и PIN")
            post("admin", f"/settings/users/{st_.id}/cards", {"uid": "7799990001", "label": "пропуск"}, what="Карта стажёру")
            cd = _first(M.StaffCard, M.StaffCard.user_id == st_.id)
            if cd:
                post("admin", f"/settings/users/{st_.id}/cards/{cd.id}/toggle", what="Карту выключить")
            get("admin", f"/settings/users/{st_.id}/dismiss", what="Увольнение — экран")
            post("admin", f"/settings/users/{st_.id}/dismiss", {"reason": "проверка"}, what="Уволить стажёра")
            get("admin", f"/settings/users/{st_.id}/archive", what="Архив сотрудника")
            post("admin", f"/settings/users/{st_.id}/rehire", what="Вернуть стажёра")
            post("admin", f"/settings/users/{st_.id}/dismiss", {"reason": "стажировка закончилась"}, what="Уволить стажёра снова")
    # ---- настройки (сохраняем то, что уже есть — ничего не ломаем)
    with at(day, "08:20"):
        from app.services import settings as st
        with SessionLocal() as db:
            br = db.scalar(select(M.Branch))
            num = st.get(db, br.id, "order_number") or {}
            code = br.code
        post("admin", "/settings/numbering", {"template": num.get("template") or "{код}-{год}-{номер}", "code": code or "СТО1",
                                              "reset_yearly": "1" if num.get("reset_yearly") else ""}, what="Нумерация нарядов")
        post("admin", "/settings/receive-backup", {"code": "deputy", "on": ""}, what="Приёмка без кладовщика (выкл.)")
        post("admin", "/settings/keys", {"count": "50"}, what="Ячейки ключей")
        post("admin", "/settings/labels", {"size": "100x70", "mode": "label", "show_price": "1", "show_cell": "1"}, what="Наклейки и принтер")
        post("admin", "/settings/points", {"name": "Чёрный вход", "counts": "1"}, what="Точка табеля")
        post("admin", "/settings/points/unkiosk", what="Снять «киоск» с этого ПК")
        post("admin", "/api/brands", {"name": "Brembo"}, fetch=True, what="Бренд из окна запчасти")
        post("admin", "/settings/brands", {"action": "add", "name": "Ferodo Racing"}, what="Бренд: добавить")
        b = _first(M.Brand, M.Brand.name == "Ferodo Racing")
        if b:
            post("admin", "/settings/brands", {"action": "rename", "bid": str(b.id), "name": "Ferodo DS"}, what="Бренд: переименовать")
            post("admin", "/settings/brands", {"action": "delete", "bid": str(b.id)}, what="Бренд: убрать из списка")
        r = P("admin").post("/reports/template", json={"header": "СТО «Мотор»", "footer": "", "signer1": "Директор", "signer2": "Бухгалтер"})
        R.check(r.status_code == 200, "шаблон отчёта сохраняется", str(r.status_code))
    # ---- зарплата: схемы, группы, оклад, доступ заму, корректировки, выплата
    with at(day, "08:40"):
        and_ = uid("andrey")
        post("admin", f"/settings/payroll/{and_}/lines", {"kind": "percent", "base": "parts_margin", "rate": "1"},
             what="Схема: добавить строку")
        ln = _first(M.PaySchemeLine, M.PaySchemeLine.base == "parts_margin", M.PaySchemeLine.rate == 1)
        if ln:
            post("admin", f"/settings/payroll/lines/{ln.id}", {"rate": "2"}, what="Схема: поменять ставку")
            post("admin", f"/settings/payroll/lines/{ln.id}/delete", what="Схема: убрать строку")
        post("admin", "/settings/payroll/groups", {"name": "Масла и фильтры"}, what="Группа запчастей для схем")
        g = _first(M.PartGroup, M.PartGroup.name == "Масла и фильтры")
        cid_ = (_first(M.PartCatalog, M.PartCatalog.section == "to") or _first(M.PartCatalog)).id
        if g and cid_:
            post("admin", f"/settings/payroll/groups/{g.id}/items", {"catalog_id": str(cid_)}, what="Группа: добавить позицию")
            post("admin", f"/settings/payroll/groups/{g.id}/items", {"catalog_id": str(cid_), "remove": "1"}, what="Группа: убрать позицию")
        post("admin", "/settings/payroll/salary", what="Начислить оклады")
        post("deputy", "/payroll/access-request", {"reason": "Проверить выплаты мастерам"}, what="Зам просит «Зарплату сотрудников»")
        pa = _first(M.PayrollAccess, M.PayrollAccess.status == "pending")
        if pa:
            post("director", f"/payroll-access/{pa.id}", {"action": "approve", "hours": "24"}, what="Директор: открыть заму на день")
            post("director", f"/payroll-access/{pa.id}", {"action": "close"}, what="Директор: закрыть доступ досрочно")
        old = (monday(day) - timedelta(days=14)).isoformat()
        post("admin", f"/payroll/weeks/{old}/adjust", {"user_id": str(and_), "amount": "300", "kind": "bonus",
                                                    "note": "за чистоту на посту"}, what="Премия за неделю")
        acc = _first(M.PayrollAccrual, M.PayrollAccrual.user_id == and_, order=M.PayrollAccrual.created_at.desc())
        if acc:
            post("admin", f"/payroll/accruals/{acc.id}", {"amount": str(acc.amount), "reason": "проверил", "action": "save",
                                                         "d": old}, what="Начисление: правка")
        post("admin", f"/payroll/account/{and_}", {"pay_method": "card", "pay_account": "5375 4141 0000 1111",
                                                "pay_bank": "Монобанк"}, what="Реквизиты для выплаты")
        post("admin", "/payroll/payouts", {"user_id": str(uid("moyka")), "amount": "100", "method": "cash", "note": "аванс"},
             what="Выплата (аванс)")
        po = _first(M.PayrollPayout, M.PayrollPayout.note == "аванс")
        if po:
            post("admin", f"/payroll/payouts/{po.id}/delete", what="Выплата: удалить (ошиблись)")
        post("buh", f"/payroll/weeks/{old}/status", {"status": "checked", "note": ""}, what="Проверил всех (старая неделя)")
    # ---- табель: отметка вручную и удаление
    with at(day, "08:50"):
        post("admin", "/timesheet/events", {"user_id": str(uid("buh")), "day": str(day), "at": "08:00", "direction": "in",
                                            "comment": "забыла карту"}, what="Табель: отметка вручную")
        ev = _first(M.AttendanceEvent, M.AttendanceEvent.comment == "забыла карту")
        if ev:
            post("admin", f"/timesheet/events/{ev.id}/delete", what="Табель: удалить отметку")
    # ---- клиенты, машины, доступы
    with at(day, "10:20"):
        # 25.11.2026: клиент Ирины (машина у нас) — чужого менеджер не правит (так и задумано), раньше скрипт брал первого
        o_ = _first(M.WorkOrder, M.WorkOrder.manager_id == uid("irina"),
                    M.WorkOrder.status.notin_(("closed", "cancel")), order=M.WorkOrder.created_at)
        cl = one(M.Client, M.Client.id == o_.client_id) if o_ else None
        if cl:
            post("irina", f"/clients/{cl.id}/edit", {"full_name": cl.full_name, "phone": cl.phone}, what="Клиент: правка")
            post("irina", f"/clients/{cl.id}/note", {"text": "Предпочитает звонок после 17:00"}, what="Клиент: заметка")
            post("irina", f"/clients/{cl.id}/regular", what="Клиент: постоянный")
            ev = _first(M.ClientEvent, M.ClientEvent.kind == "recommendation")
            if ev:
                post("admin", f"/clients/{ev.client_id}/events/{ev.id}/resolve", what="Рекомендация: сделано")
            v = _first(M.Vehicle, M.Vehicle.client_id == cl.id)
            if v:
                post("irina", f"/vehicles/{v.id}/edit", {"make": v.make, "model": v.model, "plate": v.plate, "year": str(v.year or ""),
                                                         "vin": v.vin or "", "mileage": str(v.mileage or "")}, what="Машина: правка")
            post("director", f"/clients/{cl.id}/grant", {"user_id": str(uid("marina")), "hours": "1", "back": f"/clients/{cl.id}"},
                 what="Дать менеджеру доступ к клиенту")
        # смена владельца — машина последнего клиента (продал машину)
        cl2 = _first(M.Client, order=M.Client.created_at.desc())
        v2 = _first(M.Vehicle, M.Vehicle.client_id == cl2.id) if cl2 else None
        if v2:
            post("admin", f"/vehicles/{v2.id}/owner", {"phone": "+380671234500", "name": "Новый Владелец"}, what="Сменить владельца")
        # Марина: доступ к клиенту → отказ; ко всем нарядам → разрешили на час, закрыли; «решаю сам» — нельзя
        post("marina", "/clients/access-request", {"reason": "Клиент Ирины звонит, Ирина на обеде"}, what="Марина: доступ к клиенту")
        ca = _first(M.ClientAccess, M.ClientAccess.user_id == uid("marina"), M.ClientAccess.status == "pending")
        if ca:
            post("director", f"/client-access/{ca.id}", {"action": "decline"}, what="Доступ к клиенту: отказать")
        post("marina", "/orders/access-request", {"reason": "Ирина заболела — веду её машины"}, what="Марина: доступ ко всем нарядам")
        oa = _first(M.OrderAccess, M.OrderAccess.user_id == uid("marina"), M.OrderAccess.status == "pending")
        if oa:
            post("marina", f"/access-requests/{oa.id}/self", expect_no=True, what="«Решаю сам» — ко всем нарядам нельзя")
            post("director", f"/access-requests/{oa.id}", {"action": "approve", "hours": "1"}, what="Директор: доступ на час")
            post("director", f"/access-requests/{oa.id}", {"action": "close"}, what="Директор: закрыть доступ")
    # ---- наряды: фото, предоплата, проблемы (правка / удалить), «на диагностику» / «на согласование», инструмент
    with at(day, "10:40"):
        work = [o for o in ACTIVE if o.wo() is not None and o.wo().status == "work"]
        appr = [o for o in ACTIVE if o.wo() is not None and o.wo().status == "approve"]
        if work:
            post(work[0].mgr, f"{work[0].url}/photos", files=photo(1), fetch=True, what="Фото в наряд")
            tool = _first(M.Tool, M.Tool.holder_id.is_(None), M.Tool.category.notin_(("personal", "spare")))
            if tool:
                post("pavel", f"{work[0].url}/tools", {"tool_id": str(tool.id), "user_id": str(work[0].wo().mechanic_id)},
                     what="Выдать инструмент под наряд (из наряда)")
        if appr:
            o = appr[0]
            post(o.mgr, f"{o.url}/money", {"payment_type": "prepay", "prepay_amount": "1000", "prepay_for": "запчасти под заказ"},
                 what="Предоплата: 1000 ₴ за запчасти")
            mech = one(M.User, M.User.id == o.wo().mechanic_id) if o.wo().mechanic_id else None
            if mech:
                post(mech.login, f"{o.url}/findings", {"text": "Трещина пыльника ШРУСа", "urgency": "soon"}, fetch=True,
                     what="Мастер: новая находка")
                f = _first(M.WoFinding, M.WoFinding.work_order_id == o.id, M.WoFinding.text == "Трещина пыльника ШРУСа")
                if f:
                    post(mech.login, f"{o.url}/findings/{f.id}/edit", {"text": "Трещина пыльника внутреннего ШРУСа", "urgency": "soon"},
                         what="Проблема: исправить")
                    post(mech.login, f"{o.url}/findings/{f.id}/delete", what="Проблема: удалить (ошибся)")
        # приёмка без мастера: одна машина «на диагностику», другая — менеджер записал со слов → «на согласование»
        for k, (mgr, how) in enumerate((("marina", "diag"), ("irina", "approve"))):
            c = free_client()
            r = post(mgr, "/orders/new", {"phone": c["phone"], "name": c["name"], "plate": c["plate"], "make": c["make"],
                                          "model": c["model"], "year": str(c["year"]), "mileage": str(c["km"] + 20000),
                                          "vin": c["vin"], "scenario": "B", "complaint": "Посмотреть подвеску" if k == 0 else "Скрип справа",
                                          "partsman_id": str(uid(PARTSMEN[mgr][0])), "mechanic_id": "", "lift_id": "",
                                          "fuel_level": "50", "intake_items": ["docs"], "intake_token": f"sim-x{k}-{rnd.random()}"},
                     files=photo(1), what="Приёмка без мастера", fetch=True)
            if r is None or r.status_code != 200:
                continue
            url = r.json()["url"]
            if how == "diag":
                post(mgr, f"{url}/to-diag", {"mechanic_id": str(uid("taras")), "lift_id": ""}, what="На диагностику — Тарасу")
                post("taras", "/lifts/take", {"lift_id": str(one(M.Lift, M.Lift.mechanic_id == uid("taras")).id)}, what="Слесарь: моё место")
            else:
                post(mgr, f"{url}/findings", {"text": "Со слов клиента: скрип справа при повороте", "urgency": "soon"}, fetch=True,
                     what="Менеджер: проблема со слов клиента")
                post(mgr, f"{url}/to-approve", what="Проблемы записаны → согласование")
    # ---- запчасти: «нужна» → со склада / заказать, наценка, архив → вернуть, «в наряд» со склада
    with at(day, "11:00"):
        appr = [o for o in ACTIVE if o.wo() is not None and o.wo().status == "approve"]
        if appr:
            o = appr[-1]
            pm = one(M.User, M.User.id == o.wo().partsman_id)
            pm_l = pm.login if pm else "oleg"
            post(pm_l, f"{o.url}/parts", {"name": "Хомут выпускной", "cost": "120", "qty": "1", "action": "need"}, what="Деталь «нужна»")
            p_ = _first(M.WoPart, M.WoPart.work_order_id == o.id, M.WoPart.name == "Хомут выпускной")
            if p_:
                post(pm_l, f"{o.url}/parts/{p_.id}/source", {"action": "order"}, what="Нужна → заказать")
                post(pm_l, f"{o.url}/parts/{p_.id}/markup", {"markup_pct": "45", "cost": "120"}, what="Наценка детали")
                post(pm_l, f"{o.url}/parts/{p_.id}/delete", {"reason": ""}, what="Убрать деталь в архив")
                post(pm_l, f"{o.url}/parts/{p_.id}/restore", what="Вернуть из архива")
            it = _first(M.StockItem, M.StockItem.catalog_id == cat_id(LAMP)) if LAMP else None
            if it:
                post(pm_l, f"/stock/items/{it.id}/to-order", {"order_id": str(o.id), "qty": "1"}, what="Склад → в наряд")
    # ---- склад: категории, «подходит для», «Всегда в наличии», закупка (строка, накладная)
    with at(day, "11:20"):
        post("pavel", "/stock/categories", {"section": "repair", "name": "Выхлоп"}, what="Категория склада")
        sc = _first(M.StockCategory, M.StockCategory.name == "Выхлоп")
        if sc:
            post("pavel", f"/stock/categories/{sc.id}", {"action": "rename", "name": "Выхлопная система"}, what="Категория: переименовать")
        it = _first(M.StockItem, M.StockItem.donor_id.is_(None), order=M.StockItem.created_at)
        if it:
            c_ = one(M.PartCatalog, M.PartCatalog.id == it.catalog_id)
            post("pavel", f"/stock/items/{it.id}/category", {"section": c_.section or "repair", "category": c_.category or "",
                                                             "subcategory": c_.subcategory or ""}, what="Позиция: категория")
            post("pavel", f"/stock/items/{it.id}/fits", {"make": "Toyota", "model": "Camry", "year_from": "2012", "year_to": "2018"},
                 what="Позиция: подходит для")
            ft = _first(M.PartFit, M.PartFit.catalog_id == it.catalog_id, M.PartFit.make == "Toyota")
            if ft:
                post("pavel", f"/stock/items/{it.id}/fits/{ft.id}/delete", what="Подходит для: убрать")
        k_ = _first(M.StockKeep)
        if k_:
            post("pavel", f"/stock/always/{k_.id}", {"min_qty": str(k_.min_qty)}, what="Всегда в наличии: норма")
        d_ = _first(M.Purchase, M.Purchase.status == "draft", M.Purchase.work_order_id.is_(None))
        ln = _first(M.PurchaseLine, M.PurchaseLine.purchase_id == d_.id) if d_ else None
        if ln:
            who_ = one(M.User, M.User.id == d_.created_by)
            post(who_.login if who_ else "pavel", f"/purchases/{d_.id}/lines/{ln.id}", {"qty": str(ln.qty), "price": str(ln.price),
                                                                                       "action": "save"}, what="Закупка: строка")
        o_ = _first(M.Purchase, M.Purchase.status == "ordered")
        if o_:
            post("pavel", f"/purchases/{o_.id}/delivered", {"invoice": "ТТН-77001"}, what="№ накладной у закупки")
    # ---- инструмент: разложить, карточка, «забрал у окошка», отменить запрос
    with at(day, "11:40"):
        sh = _first(M.ToolShelf)
        tl = _first(M.Tool, M.Tool.shelf_id.is_not(None)) or _first(M.Tool)
        if sh and tl:
            post("pavel", "/tools/place", {"shelf_id": str(sh.id), "tool": [str(tl.id)]}, what="Разложить по полкам")
            post("pavel", f"/tools/{tl.id}", {"action": "save", "name": tl.name, "category": tl.category or "shared",
                                              "condition": getattr(tl, "condition", "") or "ok"},
                 files=[("photo", ("tool.jpg", PHOTOS[0].read_bytes(), "image/jpeg"))], what="Инструмент: карточка с фото")
        wk = [o for o in ACTIVE if o.wo() is not None and o.wo().status in ("work", "instock", "waiting")]
        # общий инструмент без запросов — заводим два новых в шкаф (на старые уже есть запросы на сегодня)
        for nm in ("Тестер аккумуляторов", "Набор щупов"):
            post("pavel", "/tools", {"name": nm, "category": "electric", "cost": "1800", "place": "Шкаф 1"}, what=f"Инструмент: завести «{nm}»")
        free_t = [t for t in q(M.Tool, M.Tool.holder_id.is_(None), M.Tool.name.in_(("Тестер аккумуляторов", "Набор щупов")))]
        wo_t = _first(M.WorkOrder, M.WorkOrder.status.in_(("work", "instock", "waiting")), M.WorkOrder.mechanic_id == uid("andrey"))
        if wo_t and len(free_t) >= 2:
            url_t = f"/orders/{wo_t.id}"
            m_ = one(M.User, M.User.id == wo_t.mechanic_id)
            mgr_t = one(M.User, M.User.id == wo_t.manager_id)
            if m_ and mgr_t:
                post(m_.login, f"{url_t}/tool-requests", {"tool_id": str(free_t[0].id)}, what="Попросить инструмент (заберёт менеджер)")
                tr = _first(M.ToolRequest, M.ToolRequest.status == "new", M.ToolRequest.tool_id == free_t[0].id)
                if tr:
                    post("pavel", f"/tool-requests/{tr.id}/ready", what="Инструмент: готово к выдаче")
                    post(mgr_t.login, f"/tool-requests/{tr.id}/pickup", what="Менеджер: забрал у окошка — выдано")
                post(m_.login, f"{url_t}/tool-requests", {"tool_id": str(free_t[1].id)}, what="Попросить ещё инструмент")
                tr2 = _first(M.ToolRequest, M.ToolRequest.status == "new", M.ToolRequest.tool_id == free_t[1].id)
                if tr2:
                    post("pavel", f"/tool-requests/{tr2.id}/cancel", what="Кладовщик: отменил запрос (мастер передумал)")
    # ---- разбор: справочник, службы доставки, донор, деталь (перенести, списать, удалить), продажа с экрана «Продажи»
    with at(day, "12:10"):
        post("dmitro", "/razbor/refs", {"action": "make_add", "name": "Peugeot"}, what="Разбор: марка")
        mk = _first(M.RazborMake, M.RazborMake.name == "Peugeot")
        if mk:
            post("dmitro", "/razbor/refs", {"action": "make_rename", "rid": str(mk.id), "name": "Peugeot (FR)"}, what="Разбор: переименовать марку")
            post("dmitro", "/razbor/refs", {"action": "make_del", "rid": str(mk.id)}, what="Разбор: удалить марку")
        post("dmitro", "/razbor/carriers", {"name": "Делівері", "kind": "post", "phone": "0800 500 000"}, what="Служба доставки")
        dn = _first(M.Donor)
        if dn:
            post("dmitro", f"/razbor/donors/{dn.id}", {"make": dn.make, "model": dn.model, "vin": dn.vin or "", "year": str(dn.year or ""),
                                                      "mileage": str(dn.mileage or ""), "purchase_price": str(dn.purchase_price or 0),
                                                      "expenses": str(dn.expenses or 0), "notes": "Двигатель на запчасти"},
                 what="Донор: правка")
        parts = [i for i in q(M.StockItem, M.StockItem.donor_id.is_not(None)) if (i.price_state or "") == "" and free_qty(i.catalog_id) > 0]
        if len(parts) >= 3:
            post("dmitro", f"/razbor/parts/{parts[0].id}/move", {"catsub": "", "back": "/razbor"}, what="Деталь разбора: перенести")
            post("dmitro", f"/razbor/parts/{parts[1].id}/writeoff", {"qty": "1", "reason": "Треснула при снятии"}, what="Деталь разбора: списать")
        if dn:
            post("dmitro", f"/razbor/donors/{dn.id}/parts", {"name": "Заглушка бампера (тест)", "price": "150", "qty": "1"},
                 what="Деталь для удаления")
            tmp = _first(M.StockItem, M.StockItem.donor_id == dn.id, order=M.StockItem.created_at.desc())
            if tmp:
                post("dmitro", f"/razbor/parts/{tmp.id}/delete", {"back": "/razbor"}, what="Деталь разбора: удалить")
        sale_it = [i for i in parts[2:] if free_qty(i.catalog_id) > 0]
        if sale_it:
            post("dmitro", "/razbor/sales/new", {"item_id": str(sale_it[0].id), "qty": "1", "buyer_name": "Покупатель Тест",
                                                 "buyer_phone": "+380631112233", "delivery": "pickup", "price": str(sale_it[0].sale_price),
                                                 "pay_method": "cash", "mode": "reserve"}, what="Продажа с экрана «Продажи»")
    # ---- мойка: прайс, бокс, химия (отмена), очередь (↑ / ↓)
    with at(day, "12:30"):
        post("irina", "/wash/prices", {"name": "Чистка дисков", "kind": "wash", "price_car": "250", "minutes": "20"}, what="Прайс мойки: услуга")
        bx = _first(M.WashBox)
        if bx:
            post("irina", f"/wash/boxes/{bx.id}", {"name": bx.name or "Мойка 1", "active": "1"}, what="Бокс мойки")
        chem_ask("moyka")
        chem_ask("moyka")
        cr = _first(M.ChemRequest, M.ChemRequest.status == "new", order=M.ChemRequest.created_at.desc())
        if cr:
            post("moyka", f"/wash/requests/{cr.id}/cancel", what="Мойщик: отменил запрос химии")
        cr2 = _first(M.ChemRequest, M.ChemRequest.status == "new", order=M.ChemRequest.created_at.desc())
        if cr2:
            post("pavel", f"/chem/{cr2.id}/decline", {"reason": "Нет на складе — заказали"}, what="Кладовщик: отказать в химии")
        from app.services import wash as wsvc
        with SessionLocal() as db:
            qq = wsvc.board(db, db.scalar(select(M.Branch.id)))["queue"]
            ids = [c.o.id for c in qq]
        if len(ids) >= 3:
            post("irina", f"/wash/{ids[2]}/move", {"where": "up"}, what="Очередь мойки: выше")
            post("irina", f"/wash/{ids[1]}/up", what="Очередь мойки: ↑")
    # ---- передача наряда, «открыть заново», гарантия, мастер просит деталь со склада
    with at(day, "12:35"):
        appr = [o for o in ACTIVE if o.wo() is not None and o.wo().status == "approve" and o.mgr == "irina"]
        if appr:
            o = appr[0]
            wo = o.wo()
            post("irina", f"{o.url}/assign", {"partsman_id": str(wo.partsman_id or ""), "mechanic_id": str(wo.mechanic_id or ""),
                                              "lift_id": "", "manager_id": str(uid("marina"))}, what="Передать наряд Марине")
            post("marina", f"{o.url}/handover-ack", what="Марина: ✓ Принял наряд")
            o.mgr = "marina"
        closed = q(M.WorkOrder, M.WorkOrder.status == "closed", M.WorkOrder.manager_id == uid("irina"),
                   order=M.WorkOrder.closed_at.desc())
        busy = {r.work_order_id for r in q(M.OrderReopen, M.OrderReopen.status == "pending")}
        closed = [w for w in closed if w.id not in busy]
        if len(closed) >= 2:
            # 26.11.2026: «решаю сам» — только наряд, закрытый ≤ суток назад (по настоящим часам: время сдвигается после шага)
            def _fresh(w):
                ca = w.closed_at if w.closed_at.tzinfo else w.closed_at.replace(tzinfo=timezone.utc)
                return datetime.now(timezone.utc) - ca <= timedelta(hours=23)
            fresh = [w for w in closed if w.closed_at and _fresh(w)]
            a = fresh[0] if fresh else closed[0]
            b = next(w for w in closed if w.id != a.id)
            post("irina", f"/orders/{a.id}/reopen-request", {"reason": "Клиент просит добавить работу"}, what="Попросить открыть заново")
            ra = _first(M.OrderReopen, M.OrderReopen.work_order_id == a.id, M.OrderReopen.status == "pending")
            if ra and fresh:
                # сегодня (суббота) директора и зама по табелю нет — менеджер может решить сам (наряд закрыт ≤ суток)
                post("irina", f"/reopen-requests/{ra.id}/self", what="«Директора нет — решаю сам»")
                if one(M.OrderReopen, M.OrderReopen.id == ra.id).status == "pending":
                    post("director", f"/reopen-requests/{ra.id}", {"action": "decline"}, what="Директор: отказать открыть заново")
            post("irina", f"/orders/{b.id}/warranty-request", {"reason": "Через неделю снова стук спереди"}, what="Ремонт по гарантии: запрос")
            rb = _first(M.OrderReopen, M.OrderReopen.work_order_id == b.id, M.OrderReopen.status == "pending")
            if rb:
                post("director", f"/reopen-requests/{rb.id}", {"action": "approve"}, what="Директор: открыть по гарантии")
                post("irina", f"/orders/{b.id}/warranty-done", {"action": "reject", "result": "Повреждение после ДТП — не гарантия"},
                     what="Не гарантийный случай")
        wk = [o for o in ACTIVE if o.wo() is not None and o.wo().status in ("work", "instock", "waiting") and o.wo().mechanic_id == uid("andrey")]
        it = [i for i in q(M.StockItem, M.StockItem.donor_id.is_(None)) if free_qty(i.catalog_id) > 0]
        if wk and it:
            post("andrey", "/messages/ask", {"item_id": str(it[0].id), "order_id": str(wk[0].id), "qty": "1",
                                             "text": "Нужна ещё одна"}, what="Мастер: попросить деталь со склада")
            m = _first(M.OrderMessage, M.OrderMessage.kind == "part", M.OrderMessage.status == "pending",
                       order=M.OrderMessage.created_at.desc())
            if m:
                post(wk[0].mgr, f"/messages/{m.id}/decide", {"action": "decline", "answer": "Клиент не согласовал",
                                                             "back": "/ready"}, what="Менеджер: отказать в детали")
    # ---- «Запись»: мастер отметил «Видел»
    with at(day, "12:40"):
        ap = _first(M.Appointment, M.Appointment.mechanic_id == uid("elektro"), M.Appointment.seen_key == "")
        if ap:
            post("elektro", f"/schedule/{ap.id}/seen", {"back": "/schedule"}, what="Запись: «Видел»")


def screen_tour():
    """23.11.2026 (брат): каждый экран CRM под каждым сотрудником — открывается без ошибки (403 «нет доступа» — норма)."""
    import re as _re
    br = one(M.Branch)
    ids = {
        "order_id": (_first(M.WorkOrder, M.WorkOrder.status == "work") or _first(M.WorkOrder)).id,
        "client_id": _first(M.Client).id, "user_id": uid("andrey"), "uid": uid("andrey"),
        "d": (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat(), "pid": _first(M.Purchase).id,
        "photo_id": (_first(M.WoPhoto) or _first(M.StockPhoto)).id if (_first(M.WoPhoto) or _first(M.StockPhoto)) else None,
        "role_id": _first(M.Role).id, "tid": _first(M.Tool).id if _first(M.Tool) else None,
        "cid": _first(M.ToolCheck).id if _first(M.ToolCheck) else None,
        "did": _first(M.Donor).id if _first(M.Donor) else None,
        "iid": (_first(M.StockItem, M.StockItem.donor_id.is_not(None)) or _first(M.StockItem)).id,
        "item_id": _first(M.StockItem, M.StockItem.donor_id.is_(None)).id, "fmt": "json",
        "wo_id": (_first(M.Purchase, M.Purchase.work_order_id.is_not(None)) or _first(M.Purchase)).work_order_id,
    }
    # файлы фото — первые из папок разбора и инструмента
    from app.services import razbor as _rz
    from app.services import tools as _tl

    def _file(d):
        try:
            return next((p.name for p in Path(d).iterdir() if p.is_file()), None)
        except OSError:
            return None
    files = {"razbor": _file(_rz.PHOTO_DIR), "tools": _file(_tl.TOOL_PHOTO_DIR)}
    urls = []
    for r in app.routes:
        if "GET" not in (getattr(r, "methods", None) or ()) or r.path in SKIP_ROUTES or r.path in ("/notifications", "/login"):
            continue
        path = r.path
        if "{name}" in path:
            fn = files["razbor" if "razbor" in path else "tools"]
            if not fn:
                continue
            path = path.replace("{name}", fn)
        ok = True
        for p in _re.findall(r"\{(\w+)\}", path):
            v = ids.get(p)
            if v is None:
                ok = False
                break
            path = path.replace("{" + p + "}", str(v))
        if ok:
            urls.append(path)
    # 25.11.2026 (брат): плитки-окна — каждый вид отдельно (адрес тот же, содержимое разное)
    urls += [f"/timesheet?v={v}" for v in ("on", "out", "none", "info", "boss", "open", "hours", "late", "warn", "over")]
    urls += ["/tools?view=reqs", "/tools?view=late", "/tools?view=cabinets", "/tools?view=spare", "/tools?view=list",
             "/wash?tab=chem", "/wash?tab=buy", "/wash?tab=asks"]
    urls += [f"/purchases/by-partsman?st={s_}" for s_ in ("wait", "partial", "late", "arrived")]
    urls += [f"/my-orders?st={s_}" for s_ in ("todo", "wait", "partial", "late", "arrived")]
    logins = list(STAFF) + [s[0] for s in STAFF_EXTRA]
    bad = []
    for login in logins:
        for u in urls:
            try:
                r = P(login).get(u)
            except Exception as e:
                bad.append((login, u, f"{type(e).__name__}: {e}"[:200]))
                continue
            if r.status_code >= 500:
                bad.append((login, u, str(r.status_code)))
    for login, u, why in bad:
        R.fail(login, "экран " + u, u, "не открылся: " + why)
    print(f"\nОбход экранов: {len(urls)} экранов × {len(logins)} сотрудников, ошибок {len(bad)}")
    _ = br


def run():
    t0 = _time.time()
    prepare_staff()
    with TestClient(app):   # запуск программы: таблицы, роли, тестовые входы
        pass
    for lg in STAFF:
        PASS[lg] = lg + "123"
    setup()
    # 25.11.2026 (брат): правила табеля (опоздания, неявки, «забыли уход») — с прошлого понедельника, а не с дня запуска
    with SessionLocal() as db_:
        from app.db import current_branch_id as _cb
        from app.services import settings as _st
        br_ = db_.scalar(select(M.Branch))
        _cb.set(br_.id)
        _st.put(db_, br_.id, "attendance", {"alerts_from": (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat()})
        db_.commit()
    first_stock()
    appts = []
    wd = -1   # 22.11.2026: рабочий день по счёту (сб и вс — выходные; сегодня — «вышли в субботу»)
    for n_ in range(DAYS + 1):
        day = START + timedelta(days=n_)
        R.day = day
        if day.weekday() >= 5 and day != TODAY:
            continue
        wd += 1
        n = wd
        print(f"\r{day} · активных нарядов {len(ACTIVE)} · ошибок {len(R.fails)}   ", end="", flush=True)
        attendance(day)
        if day.weekday() == 0 and n > 0 and day < TODAY:
            payroll_week(day)
        if n == 1:
            razbor_start(day)
        if 2 <= n and day < TODAY and rnd.random() < 0.8:
            razbor_sale(day)
        if n == 3:
            tool_check(day)
        if n == 1:
            keep_setup(day)
        if n % 3 == 1:   # пополнение склада: кладовщик — «Всегда в наличии» → «Заказать недостающее» (17.11.2026)
            restock(day)
        if n == 5:
            with at(day, "18:00"):   # инвентаризация одной позиции
                it = one(M.StockItem, M.StockItem.catalog_id == cat_id(LAMP))
                if it:
                    post("pavel", f"/stock/items/{it.id}/adjust", {"actual": str(max(0, int(free_qty(it.catalog_id)) - 1)),
                                                                   "reason": "Пересчёт: одна разбита"}, what="Инвентаризация")
        deliveries(day)
        # Записанные на сегодня приехали
        for a in [a for a in appts if a["day"] == day]:
            with at(day, f"{a['hh']:02d}:00"):
                post(a["mgr"], f"/schedule/{a['id']}/status", {"status": "arrived", "view": "day"}, what="Приехал по записи")
            new_order(day, a["mgr"], f"{a['hh']:02d}:05", from_appt=a)
        # 20.11.2026 (брат): всего 20–30 машин — за прошлые дни по 1–2 (насыщенные наряды), сегодняшние машины
        # заводит leave_for_today (каждая — под свою кнопку)
        if day < TODAY:   # 22.11.2026 (брат): плотнее — 2–3 машины в день
            for _ in range((3, 2, 3, 3, 2)[n % 5]):
                new_order(day, rnd.choice(MANAGERS), f"{rnd.randint(8, 12):02d}:{rnd.randint(0, 59):02d}")
        if day < TODAY and n in (2, 4, 7):   # быстрая мойка за прошлые дни (сегодняшние — в wash_today)
            quick_wash(day, f"{rnd.randint(9, 15):02d}:{rnd.randint(0, 59):02d}")
        # Двигаем наряды по шагам
        approve_left = [0]
        for o in list(ACTIVE):
            hh = f"{rnd.randint(9, 16):02d}:{rnd.randint(0, 59):02d}"
            if getattr(o, "last", None) and o.last[0] == day and o.last[1] >= hh:   # не раньше, чем машину приняли
                hh = _later(o.last[1], 20)
            if day == TODAY and getattr(o, "born", None) == TODAY:
                # Приняли сегодня: часть ждёт диагностики (мастер), две — согласования (менеджер),
                # половина «без диагностики» — с ценами, ждёт подбора (запчастист), остальные — цены ставит менеджер
                TMAX[0] = ""
                if o.step == "diag" and approve_left[0] < 2 and o.wo().status == "diag":
                    step_diag(o, day, hh, approve=False)
                    approve_left[0] += 1
                elif o.step == "price" and rnd.random() < 0.5:
                    step_price(o, day, hh)
                continue
            for _ in range(3):   # за день — до трёх шагов
                before = o.step
                if day == TODAY and o.step in ("issue", "pick", "approve", "pay") and rnd.random() < (0.85 if o.step == "issue" else 0.6):
                    break   # сегодня часть нарядов ждёт выдачи / подбора / оплаты — их доделает человек
                TMAX[0] = ""
                try:
                    advance(o, day, hh)
                    hh = max(hh, TMAX[0])
                    o.last = (day, hh)
                except Exception as e:   # ошибка в самом скрипте — тоже в отчёт, идём дальше
                    R.fail("скрипт", f"шаг «{o.step}»", o.url, f"{type(e).__name__}: {e}"[:300])
                    o.step = "done"
                if o.step in ("done",):
                    ACTIVE.remove(o)
                    DONE.append(o)
                    break
                if o.step == before or o.step in ("wait_parts",):
                    break
                hh = _later(hh, rnd.randint(30, 120))
                if day == TODAY and rnd.random() < 0.5:
                    break   # сегодня — остаются наряды на всех этапах
        if day < TODAY:
            chem_desk(day)
        if day < TODAY and (TODAY - day).days <= 3 and not SITE_DONE[0]:
            site_history(day)   # 22.11.2026: «На сайт» — отказы → «Продать», директор, фото, продажа
        appts += schedule_tomorrow(day)
        if day == TODAY:
            leave_for_today(day)
            site_today(day)
            schedule_next_week(day)
            queue_today(day)
            cover_extras(day)   # 23.11.2026: каждая кнопка CRM
            today_1125(day)     # 25.11.2026: напомнить об уходе, «Заказать» по норме (склад / химия), новые окна
            check_badges()
            break
        end_of_day(day)
    print()
    final_checks()
    write_report(_time.time() - t0)


def advance(o, day, hh):
    s = o.step
    if s == "diag":
        step_diag(o, day, hh)
    elif s == "price":
        step_price(o, day, hh)
    elif s == "pick":
        step_pick(o, day, hh)
    elif s == "approve":
        step_approve(o, day, hh)
    elif s == "wait_parts":
        side_works(o, day, hh)
        st = o.wo().status
        if st == "instock":
            o.step = "issue"
        elif st == "work":
            o.step = "work"
    elif s == "issue":
        step_issue(o, day, hh)
    elif s == "work":
        step_work(o, day, hh)
    elif s == "qwash":
        step_qwash(o, day, hh)
    elif s == "pay":
        step_pay(o, day, hh)
    elif s == "reopen":
        step_reopen(o, day, hh)
    elif s == "cancel":
        step_cancel(o, day, hh)


def today_1125(day):
    """25.11.2026 (брат): новые кнопки и окна — менеджер напоминает «отметьте уход» (одному, остальные ждут — 🔴),
    кладовщик ставит норму химии, запчастист заказывает её по норме (вторая позиция остаётся ниже нормы — 🟠),
    кладовщик «Заказать» одну позицию склада; окна «Просят / сдают», «Заказы запчастистов» по вкладкам и людям."""
    with at(day, "12:30"):
        mgr = MANAGERS[0]
        r = get(mgr, "/timesheet?v=open", what="Табель: забыли отметить уход")
        with SessionLocal() as db:
            from app.services import lateness as _ls
            br = db.scalar(select(M.Branch))
            fl = [f for f in _ls.no_out_open(db, br.id) if f.user.login != mgr]
        if fl:
            post(mgr, "/timesheet/remind", {"user_id": str(fl[0].user.id), "day": fl[0].day.isoformat()},
                 what="Напомнить: отметить уход")
            with SessionLocal() as db:
                n = db.scalar(select(func.count()).select_from(M.Notification).where(
                    M.Notification.user_id == fl[0].user.id, M.Notification.kind == "att_remind"))
            R.check(n >= 1, "напоминание «отметьте уход» пришло сотруднику", fl[0].user.full_name)
        get(mgr, "/timesheet?v=hours", what="Табель: часы (свои)")
        get(mgr, "/tools?view=reqs", what="Инструмент: просят / сдают")
        for st_ in ("wait", "partial", "late", "arrived"):
            get(mgr, f"/purchases/by-partsman?st={st_}", what="Заказы запчастистов: вкладка")
        # химия «всегда в наличии»: норма выше остатка у двух позиций
        chem = q(M.PartCatalog, M.PartCatalog.section == "chem")[:2]
        for c in chem:
            post("pavel", "/stock/always", {"catalog_id": str(c.id), "min_qty": str(int(free_qty(c.id)) + 4)},
                 what="Всегда в наличии: норма химии")
        if chem:
            r = post("oleg", "/stock/always/order/missing", {"cid": str(chem[0].id), "back": "/wash?tab=chem"},
                     what="Заказать химию по норме")
            R.check(r is not None and "/purchases/" in r.url.path, "запчастист заказал химию по норме", chem[0].name)
            r2 = post("oleg", "/stock/always/order/missing", {"cid": str(chem[0].id), "back": "/wash?tab=chem"},
                      what="Повторный заказ той же химии (не должен создаться)")
            with SessionLocal() as db:
                n = db.scalar(select(func.count()).select_from(M.PurchaseLine).join(M.Purchase, M.Purchase.id == M.PurchaseLine.purchase_id)
                              .where(M.PurchaseLine.catalog_id == chem[0].id, M.Purchase.status == "draft"))
            R.check(n == 1, "повторно та же позиция не заказывается", f"черновиков: {n}")
        get("oleg", "/wash?tab=chem", what="Мойка: химия, норма и заказ")


def keep_setup(day):
    """17.11.2026 (брат): кладовщик ведёт «Всегда в наличии» — ходовые позиции склада с нормой «не меньше N»."""
    with at(day, "08:40"):
        for i, (name, art, brand, qty, cost) in enumerate(STOCK[:10]):
            cid = cat_id(name)
            if cid:   # первым трём — норма выше остатка: будет что дозаказать («Заказать недостающее»)
                mn = qty + 4 if i < 3 else max(qty // 2, 4)
                post("pavel", "/stock/always", {"catalog_id": str(cid), "min_qty": str(mn)}, what="Всегда в наличии: норма")
        r = get("pavel", "/stock/always", what="Всегда в наличии")
        R.check(r is not None and r.status_code == 200, "«Всегда в наличии» открывается у кладовщика")


def restock(day):
    """17.11.2026 (брат): пополнение склада — кладовщик сам: «Всегда в наличии» → «Заказать недостающее» → черновик
    на него → поставщик, дата → «Заказан». Принимает потом в «Приходе» с № накладной (как все)."""
    with at(day, "09:15"):
        r = post("pavel", "/stock/always/order/missing", what="Заказать недостающее")
        path = r.url.path if r is not None else ""
        if not path.startswith("/purchases/"):
            return   # всего хватает
        pid = path.rsplit("/", 1)[1]
        post("pavel", f"/purchases/{pid}", {"action": "save", "supplier_new": "Автотехнікс",
                                            "expected_on": str(day + timedelta(days=2))}, what="Поставщик и дата")
        post("pavel", f"/purchases/{pid}/ordered", {}, what="✓ Заказан (на склад)")
        p = one(M.Purchase, M.Purchase.id == uuid.UUID(pid))
        R.check(p is not None and p.status == "ordered" and p.created_by == uid("pavel") and not p.work_order_id,
                "заказ на склад — на кладовщика, без наряда", pid)
        doc = get("pavel", f"/purchases/{pid}/print", what="Заказ на склад (печать)")
        R.check(doc is not None and "Заказ на склад" in doc.text, "«Заказ на склад» печатается", pid)


# ================================================================ проверки логики и экраны
def final_checks():
    R.day = TODAY
    from app.services import money as msvc
    from app.services import stock as ssvc
    with SessionLocal() as db:
        br = db.scalar(select(M.Branch))
        bal = ssvc.balances(db, br.id)
        neg = [(k, v) for k, v in bal.items() if v["on_hand"] < 0 or v["free"] < 0]
        R.check(not neg, "остатки склада не уходят в минус", f"позиций в минусе: {len(neg)}")
        for wo in db.scalars(select(M.WorkOrder).where(M.WorkOrder.status == "closed")):
            t = msvc.totals(db, wo)
            R.check(t.paid == t.total, "у выданной машины оплачено ровно «Итого»", f"{wo.number}: итого {t.total}, оплачено {t.paid}")
        for w in db.scalars(select(M.WoWork).join(M.WorkOrder, M.WorkOrder.id == M.WoWork.work_order_id)
                            .where(M.WoWork.status == "done", M.WorkOrder.status == "closed", M.WoWork.price > 0)):
            n = db.scalar(select(func.count()).select_from(M.PayrollAccrual).where(M.PayrollAccrual.wo_work_id == w.id)) \
                if hasattr(M.PayrollAccrual, "wo_work_id") else 1
            R.check(n > 0, "у выполненной работы есть начисление исполнителю", f"{w.name} ({w.id})")
        # 13.11.2026 (брат): без цены — ни в наряде, ни на складе
        zero = db.scalars(select(M.WoPart).where(M.WoPart.status.notin_(("returned", "declined", "archived")),
                                                 M.WoPart.price <= 0)).all()
        R.check(not zero, "у каждой запчасти в наряде есть цена", ", ".join(p.name for p in zero[:5]))
        zero_si = [i for i in db.scalars(select(M.StockItem))
                   if (i.sale_price if i.donor_id else i.cost) is None or (i.sale_price if i.donor_id else i.cost) <= 0]
        R.check(not zero_si, "у каждой позиции склада есть цена", str(len(zero_si)))
        chem_parts = db.scalar(select(func.count()).select_from(M.WoPart).join(M.PartCatalog, M.PartCatalog.id == M.WoPart.catalog_id)
                               .where(M.PartCatalog.section == "chem"))
        R.check(not chem_parts, "химия мойщика не попадает в счёт клиенту", f"строк химии в нарядах: {chem_parts}")
        for p in db.scalars(select(M.Purchase).where(M.Purchase.status == "arrived")):
            left = [l for l in db.scalars(select(M.PurchaseLine).where(M.PurchaseLine.purchase_id == p.id)) if not l.received]
            R.check(not left, "у принятой закупки все строки приняты", str(p.id))
        # 20.11.2026 (брат): машин на СТО 20–30 (машины разбора не считаются)
        cars = db.scalar(select(func.count(func.distinct(M.WorkOrder.vehicle_id))))
        orders = db.scalar(select(func.count()).select_from(M.WorkOrder))
        # 26.11.2026: нарядов больше вместе с историей; машин — как было (клиенты возвращаются)
        f_ = DAYS / 13
        lo_c, hi_c, lo_o, hi_o = 30, 55, round(45 * f_), round(90 * f_)
        R.check(lo_c <= cars <= hi_c and lo_o <= orders <= hi_o, f"машин на СТО {lo_c}–{hi_c}, нарядов {lo_o}–{hi_o}",
                f"машин {cars}, нарядов {orders}")
        print(f"\nМашин: {cars}, нарядов: {orders}")
    # Каждый экран каждого сотрудника открывается
    menu_pages()
    screen_tour()   # 23.11.2026: все экраны CRM под всеми сотрудниками


def menu_pages():
    from app.permissions import MENU, effective_permissions, menu_for
    with SessionLocal() as db:
        users = db.scalars(select(M.User).where(M.User.active.is_(True))).all()
        menus = {u.login: [m["href"] for m in menu_for(effective_permissions(db, u))] for u in users}
    for login, hrefs in menus.items():
        if login not in PASS and login not in dict((s[0], 1) for s in STAFF_EXTRA):
            continue
        for h in hrefs + ["/more"]:   # «Сообщения» не открываем — иначе сегодняшние станут прочитанными
            if h == "/notifications":
                continue
            r = get(login, h)
            if r is not None and r.status_code == 200 and FLASH_ERR.search(r.text):
                R.fail(login, "открыть " + h, h, "красное сообщение при открытии")
        # и все вкладки внутри (ссылки страницы на тот же раздел)
        for h in [x for x in hrefs if x != "/notifications"][:20]:
            r = get(login, h)
            if r is None or r.status_code != 200:
                continue
            for sub in sorted(set(re.findall(r'href="(' + re.escape(h) + r'\?[^"#]*)"', r.text)))[:12]:
                get(login, sub.replace("&amp;", "&"))


def write_report(sec):
    with SessionLocal() as db:
        stats = {name: db.scalar(select(func.count()).select_from(model)) for name, model in (
            ("нарядов", M.WorkOrder), ("работ", M.WoWork), ("запчастей в нарядах", M.WoPart), ("оплат", M.Payment),
            ("закупок", M.Purchase), ("движений склада", M.StockMovement), ("запросов химии", M.ChemRequest),
            ("отметок табеля", M.AttendanceEvent), ("начислений зарплаты", M.PayrollAccrual), ("выплат", M.PayrollPayout),
            ("продаж разбора", M.UsedSale), ("записей журнала", M.AuditLog))}
        by_status = Counter(s for s, in db.execute(select(M.WorkOrder.status)))
    out = [f"# Живой прогон CRM — отчёт ({datetime.now():%d.%m.%Y %H:%M})", "",
           f"Период: {START:%d.%m} – {TODAY:%d.%m.%Y} ({DAYS} дн.). Время прогона: {sec / 60:.1f} мин.",
           f"Нажато кнопок без ошибок: **{sum(R.ok.values())}** · не сработало: **{len(R.fails)}** · "
           f"проверок логики не прошло: **{len(R.logic)}**", "",
           "Создано: " + " · ".join(f"{k} {v}" for k, v in stats.items()), "",
           "Наряды по этапам: " + " · ".join(f"{k} {v}" for k, v in sorted(by_status.items())), ""]
    out += ["## Что не сработало (кнопки)", ""]
    groups = defaultdict(list)
    for d, who, what, path, prob in R.fails:
        key = (what, re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", ":id", path), re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", ":id", prob)[:160])
        groups[key].append((d, who))
    if not groups:
        out.append("Всё сработало.")
    for (what, path, prob), hits in sorted(groups.items(), key=lambda x: -len(x[1])):
        who = ", ".join(sorted({h[1] for h in hits}))
        out.append(f"- **{what}** — {prob}  \n  `{path}` · раз: {len(hits)} · кто: {who} · впервые {hits[0][0]:%d.%m}")
    out += ["", "## Проверки логики, которые не прошли", ""]
    lg = defaultdict(list)
    for d, what, det in R.logic:
        lg[what].append((d, det))
    if not lg:
        out.append("Все прошли.")
    for what, hits in sorted(lg.items(), key=lambda x: -len(x[1])):
        ex = "; ".join(h[1] for h in hits[:3])
        out.append(f"- **{what}** — раз: {len(hits)}. Примеры: {ex}")
    out += ["", "## Кто сколько нажал", "", " · ".join(f"{k} {v}" for k, v in R.ok.most_common())]
    out += ["", "## Цифры в меню на сегодня (что ждёт каждого)", ""]
    for login, b in BADGES.items():
        items = [f"{href}: " + "; ".join(v) for href, v in b.items()]
        out.append(f"- **{NAMES.get(login, login)}** (`{login}`): " + (" · ".join(items) if items else "— ничего"))
    # 23.11.2026 (брат): покрытие кнопок и экранов
    all_routes = sorted({(m, r.path) for r in app.routes if hasattr(r, "methods") and r.path not in SKIP_ROUTES
                         for m in (r.methods or ()) if m in ("GET", "POST")})
    posts = [x for x in all_routes if x[0] == "POST"]
    gets = [x for x in all_routes if x[0] == "GET"]
    miss_p = [p for m, p in posts if not HIT[("POST", p)]]
    miss_g = [p for m, p in gets if not HIT[("GET", p)]]
    out += ["", "## Покрытие кнопок и экранов", "",
            f"Кнопок (POST) всего {len(posts)}, нажато {len(posts) - len(miss_p)}; экранов (GET) всего {len(gets)}, "
            f"открыто {len(gets) - len(miss_g)}.", "", "### Кнопки, которые ни разу не нажимались", ""]
    out += [f"- `{p}`" for p in miss_p] or ["Все нажаты."]
    out += ["", "### Экраны, которые ни разу не открывались", ""]
    out += [f"- `{p}`" for p in miss_g] or ["Все открыты."]
    (SIM / "report.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[:12]))
    print(f"Отчёт: {SIM / 'report.md'}")


if __name__ == "__main__":
    run()
