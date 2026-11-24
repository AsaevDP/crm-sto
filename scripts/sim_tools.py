"""Имитация раздела «Инструмент» (24.11.2026, брат) — поверх рабочей базы, только кнопками, как люди.

Что делает (каждое действие — та же форма, что на экране, под тем, кто это делает в жизни):
  • кладовщик (pavel) заводит ещё 3 шкафа с полками и ~40 инструментов общего пользования по категориям,
    запасные наборы и личный инструмент каждому мастеру; отмечает «сломан» / «износ» у нескольких;
  • мастера просят инструмент под свои открытые наряды: выдано и «✓ Получил» (на руках), «готово к выдаче» —
    забирает менеджер у окошка, запрос ждёт кладовщика, мастер нажал «Сдаю» — ждёт «Принял», кладовщик «Вернул»;
  • быстрая мойка: детейлер / мойщик берёт машину, просит полировальную машинку / пылесос, работу закрывает,
    менеджер принимает оплату и выдаёт авто — инструмент остаётся у мастера после закрытия наряда (🔴),
    у одного — уже «Сдаю» (🟠);
  • ремонт «Готово к выдаче»: мастер берёт инструмент, менеджер закрывает наряд — тоже «не сдали»;
  • проверки: плановая у слесаря закрыта со сломанным «по вине» — админ утверждает удержание; у электрика проверка
    идёт («Проверки 1»); запасной набор выдан взамен сломанного личного.
Ничего не удаляет. Перед запуском — копия базы data/backup-before-tools-*.db.

Запуск (из папки проекта):  .venv\\Scripts\\python scripts\\sim_tools.py
"""
import random
import re
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# копия рабочей базы
src = ROOT / "data" / "crm.db"
bk = ROOT / "data" / f"backup-before-tools-{datetime.now():%Y%m%d-%H%M}.db"
s_, d_ = sqlite3.connect(src), sqlite3.connect(bk)
s_.backup(d_)
d_.close(), s_.close()
print("Копия базы:", bk.name)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import models as M  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.tools import TOOL_CATEGORIES  # noqa: E402
from app.services import money as msvc  # noqa: E402

rnd = random.Random(2411)
FLASH_ERR = re.compile(r'<div class="flash error"[^>]*>(.*?)</div>', re.S)
FAILS, DONE = [], []
CLIENTS = {}


def P(who):
    if who not in CLIENTS:
        c = TestClient(app)
        c.__enter__()
        r = c.post("/login", data={"login": who, "password": who + "123"}, follow_redirects=False)
        assert r.status_code == 303, f"вход {who}: {r.status_code}"
        CLIENTS[who] = c
    return CLIENTS[who]


def post(who, path, data=None, what="", files=None):
    try:
        r = P(who).post(path, data=data or {}, files=files, follow_redirects=True)
    except Exception as e:
        FAILS.append((who, what, path, f"ошибка: {e}"[:200]))
        return None
    if r.status_code >= 400:
        FAILS.append((who, what, path, f"ответ {r.status_code}"))
        return None
    m = FLASH_ERR.search(r.text)
    if m:
        FAILS.append((who, what, path, "отказ: " + re.sub(r"\s+", " ", m.group(1)).strip()))
        return None
    DONE.append((who, what))
    return r


def q(model, *cond):
    with SessionLocal() as db:
        return db.scalars(select(model).where(*cond)).all()


def one(model, *cond):
    with SessionLocal() as db:
        return db.scalars(select(model).where(*cond)).first()


def uid(login):
    return one(M.User, M.User.login == login).id


# ================================================================ 1. Шкафы, общий инструмент, запасные, личный
CABS = [("Шкаф 2 · двигатель и ходовая", "у подъёмника 1", 4), ("Шкаф 3 · электрика и диагностика", "у поста электрика", 3),
        ("Шкаф 4 · кузов, шины, мойка", "у мойки", 3)]
SHARED = [  # (название, категория, стоимость, шкаф, полка)
    ("Набор для замены ремня ГРМ VAG", "engine", 7400, 0, 0), ("Фиксатор распредвалов универсальный", "engine", 3200, 0, 0),
    ("Компрессометр бензиновый", "engine", 2100, 0, 1), ("Компрессометр дизельный", "engine", 4800, 0, 1),
    ("Эндоскоп для двигателя", "engine", 3900, 0, 1), ("Съёмник клапанов", "engine", 1800, 0, 1),
    ("Съёмник ступиц гидравлический", "chassis", 9600, 0, 2), ("Стяжки пружин (пара)", "chassis", 2600, 0, 2),
    ("Съёмник сайлентблоков", "chassis", 4200, 0, 2), ("Съёмник рулевых наконечников", "chassis", 900, 0, 2),
    ("Набор для прокачки тормозов", "brakes", 2300, 0, 3), ("Разводчик тормозных поршней", "brakes", 1700, 0, 3),
    ("Тестер тормозной жидкости", "brakes", 650, 0, 3), ("Набор для ТО: съёмники фильтров", "to", 1500, 0, 3),
    ("Сканер OBD-II Launch", "electric", 14500, 1, 0), ("Осциллограф USB", "electric", 11800, 1, 0),
    ("Тестер свечей зажигания", "electric", 1300, 1, 0), ("Пуско-зарядное устройство", "electric", 6200, 1, 1),
    ("Набор пинов для разъёмов", "electric", 850, 1, 1), ("Паяльная станция", "electric", 3400, 1, 1),
    ("Токовые клещи", "measure", 2900, 1, 2), ("Микрометр 25–50 мм", "measure", 1600, 1, 2),
    ("Нутромер индикаторный", "measure", 2400, 1, 2), ("Динамометрический ключ 5–60 Нм", "measure", 3100, 1, 2),
    ("Шиномонтажные лопатки (набор)", "tires", 700, 2, 0), ("Балансировочные грузики — инструмент", "tires", 450, 2, 0),
    ("Рихтовочный набор", "body", 5600, 2, 1), ("Споттер", "body", 8900, 2, 1), ("Полировальная машинка Makita", "body", 7800, 2, 2),
    ("Пылесос моющий Kärcher", "body", 9900, 2, 2), ("Парогенератор для салона", "body", 6400, 2, 2),
    ("Пневмогайковёрт 1/2\"", "pneumo", 5200, 0, 3), ("Пневмотрещотка 3/8\"", "pneumo", 3300, 0, 3),
    ("Съёмник подшипников 3-лапый", "pullers", 1900, 0, 2), ("Съёмник шаровых опор усиленный", "pullers", 1400, 0, 2),
    ("Набор экстракторов", "other", 800, 1, 1), ("Домкрат подкатной 3 т", "other", 6900, 2, 0),
]
SPARE = [("Запасной набор слесаря №2", 4300), ("Запасной набор слесаря №3", 4300), ("Запасной набор электрика", 3100)]
PERSONAL = {
    "andrey": [("Набор головок 1/2\" 24 шт", 2600), ("Ключи рожково-накидные 8–19", 1900), ("Отвёртки (набор 6 шт)", 450)],
    "taras": [("Набор головок 1/2\" 24 шт", 2600), ("Ключи рожково-накидные 8–19", 1900), ("Трещотка 1/2\"", 700),
              ("Молоток слесарный", 300)],
    "elektro": [("Мультиметр UNI-T", 1400), ("Стриппер для проводов", 450), ("Паяльник 60 Вт", 380)],
    "motor": [("Набор головок 1/4\"+1/2\" 94 шт", 4800), ("Щупы плоские", 220), ("Магнитный захват", 250)],
    "moyka": [("Пистолет для пены", 900), ("Щётки для дисков (набор)", 520)],
    "detail": [("Набор кистей для детейлинга", 650), ("Пистолет для химии", 480)],
}


def setup():
    if one(M.Tool, M.Tool.name == SHARED[0][0]):
        print("Инструмент уже заведён — шаг 1 пропускаю")
        return
    for name, note, n in CABS:
        post("pavel", "/tools/cabinets", {"action": "add", "name": name, "note": note, "shelves": str(n)}, what=f"Шкаф «{name}»")
    cabs = {c.name: c for c in q(M.ToolCabinet)}
    shelves = {}
    for i, (name, _n, _k) in enumerate(CABS):
        c = cabs.get(name)
        shelves[i] = sorted(q(M.ToolShelf, M.ToolShelf.cabinet_id == c.id), key=lambda s: s.sort) if c else []
    for name, cat, cost, ci, si in SHARED:
        sh = shelves.get(ci) or []
        post("pavel", "/tools", {"name": name, "category": cat, "cost": str(cost),
                                 "serial": f"SN{rnd.randint(100000, 999999)}",
                                 "shelf_id": str(sh[min(si, len(sh) - 1)].id) if sh else ""}, what=f"Инструмент «{name}»")
    for name, cost in SPARE:
        sh = shelves.get(0) or []
        post("pavel", "/tools", {"name": name, "category": "spare", "cost": str(cost), "description": "Головки, ключи, отвёртки",
                                 "shelf_id": str(sh[-1].id) if sh else ""}, what=f"Запасной «{name}»")
    for login, items in PERSONAL.items():
        for name, cost in items:
            post("pavel", "/tools", {"name": name, "category": "personal", "cost": str(cost), "holder_id": str(uid(login))},
                 what=f"Личный {login}: «{name}»")
    # состояние: два сломаны, два с износом
    for name, st in (("Тестер свечей зажигания", "broken"), ("Пневмотрещотка 3/8\"", "broken"),
                     ("Шиномонтажные лопатки (набор)", "worn"), ("Съёмник рулевых наконечников", "worn")):
        t = one(M.Tool, M.Tool.name == name)
        if t:
            post("pavel", f"/tools/{t.id}", {"action": "save", "name": t.name, "category": t.category, "cost": str(t.cost),
                                             "status": st, "shelf_id": str(t.shelf_id or "")}, what=f"«{name}» — {st}")


# ================================================================ 2. Запросы под открытые наряды
def free_tool(*cats):
    cats = cats or tuple(k for k in TOOL_CATEGORIES if k not in ("personal", "spare"))
    ts = [t for t in q(M.Tool, M.Tool.holder_id.is_(None), M.Tool.status.in_(("ok", "worn")), M.Tool.category.in_(cats))]
    busy = {r.tool_id for r in q(M.ToolRequest, M.ToolRequest.status.in_(("new", "ready")))}
    ts = [t for t in ts if t.id not in busy]
    return rnd.choice(ts) if ts else None


LOGINS = {u.id: u.login for u in q(M.User)}


def open_orders(statuses=("waiting", "instock", "work")):
    return [o for o in q(M.WorkOrder, M.WorkOrder.status.in_(statuses), M.WorkOrder.kind != "wash")
            if o.mechanic_id in LOGINS]


def ask(o, login, cats, comment=""):
    t = free_tool(*cats)
    if not t:
        return None, None
    post(login, f"/orders/{o.id}/tool-requests", {"tool_id": str(t.id), "comment": comment}, what=f"Попросить «{t.name}»")
    r = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
    return t, r


def requests():
    orders = open_orders()
    rnd.shuffle(orders)
    cats_by = {"andrey": ("engine", "chassis", "brakes", "pullers", "pneumo"), "taras": ("chassis", "brakes", "to", "pullers"),
               "elektro": ("electric", "measure"), "motor": ("engine", "measure")}
    plan = ["hands", "hands", "hands", "pickup", "wait", "returning", "returned", "hands", "wait"]
    for i, o in enumerate(orders[:len(plan)]):
        login = LOGINS[o.mechanic_id]
        t, r = ask(o, login, cats_by.get(login, ("engine", "chassis")), "под эту машину")
        if not r:
            continue
        kind = plan[i]
        if kind == "wait":
            continue                                               # просят — ждёт кладовщика
        if kind == "pickup":
            post("pavel", f"/tool-requests/{r.id}/ready", what="Готово к выдаче")
            mgr = LOGINS.get(o.manager_id)
            if mgr:
                post(mgr, f"/tool-requests/{r.id}/pickup", what="Менеджер забрал у окошка — выдано")
            continue
        post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать инструмент")
        if kind != "returned" or rnd.random() < .5:
            post(login, f"/tool-requests/{r.id}/received", what="✓ Получил")
        if kind == "returning":
            post(login, f"/tools/{t.id}/return-request?back=/tools/staff/{o.mechanic_id}", what="Сдаю")
        elif kind == "returned":
            post("pavel", f"/tools/{t.id}/returned", what="Принесли к окошку — «Вернул»")


# ================================================================ 3. Наряд закрыт — инструмент у мастера
def quick_wash_with_tool(washer, service, tool_name, press_return=False):
    svc_ = one(M.WashService, M.WashService.name == service)
    if not svc_:
        FAILS.append((washer, "мойка", "", f"нет услуги «{service}»"))
        return
    token = f"simtools-{rnd.random()}"
    n = rnd.randint(1000, 9999)
    data = {"phone": f"067{rnd.randint(1000000, 9999999)}", "name": rnd.choice(["Олег Бондар", "Ирина Сич", "Максим Гавриш",
            "Тетяна Руденко"]), "plate": f"КА {n} ТН", "make": rnd.choice(["Toyota", "Kia", "Skoda"]),
            "model": rnd.choice(["Camry", "Sportage", "Octavia"]), "car_class": "car", "washer_id": str(uid(washer)),
            "fuel_level": "50", "intake_token": token, "queue_place": "first"}
    post("irina", "/wash/new", data, files=[("service_id", (None, str(svc_.id)))], what="Приёмка на мойку")
    wo = one(M.WorkOrder, M.WorkOrder.intake_token == token)
    if not wo:
        return
    url = f"/orders/{wo.id}"
    post(washer, "/wash/take", {"order_id": str(wo.id)}, what="Взять следующую")
    t = one(M.Tool, M.Tool.name == tool_name)
    if t and not t.holder_id:
        post(washer, f"{url}/tool-requests", {"tool_id": str(t.id), "comment": "для этой машины"}, what=f"Попросить «{t.name}»")
        r = one(M.ToolRequest, M.ToolRequest.tool_id == t.id, M.ToolRequest.status == "new")
        if r:
            post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать")
            post(washer, f"/tool-requests/{r.id}/received", what="✓ Получил")
    for w in q(M.WoWork, M.WoWork.work_order_id == wo.id):
        if w.status == "todo":
            post(washer, f"{url}/works/{w.id}/state", {"action": "start"}, what="Начал")
        post(washer, f"{url}/works/{w.id}/state", {"action": "done"}, what="Выполнено")
    pay_close(wo.id, "irina")
    if press_return and t:
        post(washer, f"/tools/{t.id}/return-request?back=/tools/staff/{uid(washer)}", what="Сдаю (после закрытия)")


def pay_close(wo_id, mgr):
    url = f"/orders/{wo_id}"
    with SessionLocal() as db:
        due = msvc.totals(db, db.get(M.WorkOrder, wo_id)).due
    if due > 0:
        post(mgr, f"{url}/payments", {"amount": str(due), "method": "card", "purpose": "full",
                                      "receipt_no": str(rnd.randint(1000, 9999)), "token": f"t{rnd.random()}"}, what="Принять оплату")
    post(mgr, f"{url}/close", {"state": "ok", "defects": "", "advice": ""}, what="Выдать авто")
    st = one(M.WorkOrder, M.WorkOrder.id == wo_id).status
    if st != "closed":
        FAILS.append((mgr, "Выдать авто", url, f"наряд не закрылся: {st}"))


def repair_ready_with_tool():
    """Ремонт «Готово к выдаче»: ведущий мастер берёт инструмент (проверить после ремонта), менеджер выдаёт авто."""
    for o in open_orders(("ready",))[:1]:
        login = LOGINS[o.mechanic_id]
        t, r = ask(o, login, ("measure", "electric", "chassis"), "проверить после ремонта")
        if not r:
            continue
        post("pavel", f"/tool-requests/{r.id}/done", {"result": "ok"}, what="Выдать")
        post(login, f"/tool-requests/{r.id}/received", what="✓ Получил")
        mgr = LOGINS.get(o.manager_id)
        if mgr:
            pay_close(o.id, mgr)


# ================================================================ 4. Проверки, запасной взамен сломанного
def checks():
    taras = uid("taras")
    post("pavel", f"/tools/staff/{taras}/checks", {"kind": "planned"}, what="Плановая проверка у Тараса")
    c = one(M.ToolCheck, M.ToolCheck.user_id == taras, M.ToolCheck.status == "draft")
    if c:
        lines = q(M.ToolCheckLine, M.ToolCheckLine.check_id == c.id)
        data = {"action": "finish", "note": "Плановая проверка в конце месяца"}
        for i, l in enumerate(lines):
            data[f"r_{l.id}"] = "fault" if i == 0 else ("worn" if i == 1 else "ok")
            if i == 0:
                data[f"c_{l.id}"] = "Сломана ручка трещотки — по вине"
        post("pavel", f"/tools/checks/{c.id}", data, what="Завершить проверку (одно — по вине)")
        post("admin", f"/tools/checks/{c.id}/approve", {"approve": [str(lines[0].id)]} if lines else {},
             what="Админ утвердил удержание")
        # сломанное личное — выдаём запасной набор взамен
        sp = one(M.Tool, M.Tool.category == "spare", M.Tool.holder_id.is_(None))
        if sp:
            post("pavel", f"/tools/{sp.id}", {"action": "move", "to_user": str(taras), "comment": "Взамен сломанного"},
                 what="Запасной набор — взамен личного")
    el = uid("elektro")
    if not one(M.ToolCheck, M.ToolCheck.user_id == el, M.ToolCheck.status == "draft"):
        post("pavel", f"/tools/staff/{el}/checks", {"kind": "planned"}, what="Проверка у электрика — идёт")


if __name__ == "__main__":
    setup()
    requests()
    quick_wash_with_tool("detail", "Полировка кузова", "Полировальная машинка Makita")
    quick_wash_with_tool("moyka", "Комплексная мойка (кузов + салон)", "Пылесос моющий Kärcher", press_return=True)
    repair_ready_with_tool()
    checks()
    for c in CLIENTS.values():
        c.__exit__(None, None, None)
    print(f"Сделано действий: {len(DONE)}, не сработало: {len(FAILS)}")
    for f in FAILS:
        print("  ✕", " | ".join(str(x) for x in f))
