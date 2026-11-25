"""Архив клиентов в файлах (25.11.2026, брат): запасная копия на случай, если CRM сломается.

Первый уровень защиты — копии всей базы (services/backup.py, data/backups/). Этот архив — второй:
папка на каждого клиента, в ней
- «Клиент.xlsx» — для чтения человеком: карточка, машины, визиты, работы, запчасти, оплаты, проблемы, история нарядов;
- «данные.json» — точная копия строк базы по клиенту: по ней CRM восстанавливает историю («Настройки → Архив клиентов
  → Восстановить из архива»), даже в новую, пустую установку;
- «фото машин/» — закреплённые фото машин.
В корне архива — «_Список клиентов.xlsx» и «КАК ВОССТАНОВИТЬ.txt».

Обновляется каждую ночь (services/nightly.py) и кнопкой «Обновить сейчас». Файлы клиента переписываются, только если
что-то изменилось. Из архива возвращаются: клиенты, телефоны, машины, наряды, работы, запчасти, проблемы, оплаты,
события клиента, история наряда (журнал). НЕ возвращаются: склад, закупки, зарплата, табель, инструмент — их хранит
полная копия базы.
"""
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, types as T
from sqlalchemy.orm import Session
from sqlalchemy.types import TypeDecorator

from ..config import DATA_DIR
from ..models import (AuditLog, Client, ClientEvent, ClientPhone, Payment, User, Vehicle, WoFinding, WoPart, WoPhoto,
                      WorkOrder, WoStatusHistory, WoWork)
from ..models.orders import OrderMaster, PART_OFF, PART_STATUSES, URGENCY, WO_STATUSES
from . import settings as st

FORMAT = "crm-sto-client-archive"
VERSION = 1
JSON_NAME = "данные.json"
XLSX_NAME = "Клиент.xlsx"
# Порядок важен: при восстановлении сначала то, на что ссылаются
MODELS = [Client, ClientPhone, Vehicle, WorkOrder, OrderMaster, WoStatusHistory, WoFinding, WoWork, WoPart, WoPhoto,
          Payment, ClientEvent, AuditLog]
ORDER_CHILDREN = [OrderMaster, WoStatusHistory, WoFinding, WoWork, WoPart, WoPhoto, Payment]
AUDIT_TABLES = {"work_orders", *[m.__tablename__ for m in ORDER_CHILDREN]}
from ..models.money import PAY_METHODS  # noqa: E402

HOWTO = """КАК ВОССТАНОВИТЬ ИСТОРИЮ КЛИЕНТОВ ИЗ ЭТОГО АРХИВА

1. Скопируйте всю эту папку на флешку (лучше раз в неделю).
2. Установите CRM заново (см. инструкцию владельца).
3. Войдите как админ → «Настройки» → «Архив клиентов».
4. Скопируйте папку с флешки на компьютер с CRM и впишите путь к ней в поле «Восстановить из папки»
   (или оставьте пустым, если положили её в папку архива CRM).
5. Нажмите «Проверить» — CRM покажет, сколько клиентов и нарядов добавится. Затем «Восстановить».

Уже существующие записи не трогаются и не дублируются — восстанавливать можно повторно.
«Клиент.xlsx» — только для чтения глазами: CRM восстанавливает по файлу «данные.json», его НЕ правьте.
"""


# ---------- Папка ----------
def root(db: Session | None = None, branch_id=None) -> Path:
    folder = ""
    if db is not None and branch_id is not None:
        folder = (st.get(db, branch_id, "client_archive").get("folder") or "").strip()
    p = Path(folder) if folder else Path(os.getenv("CLIENT_ARCHIVE_DIR") or DATA_DIR / "archive")
    return p


def _safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip().rstrip(".")
    return re.sub(r"\s+", " ", s)[:80] or "без имени"


def _client_folder(base: Path, c: Client) -> Path:
    code = c.code or c.id.hex[:8]
    want = base / _safe(f"{code} {c.full_name}")
    if not want.exists() and base.exists():
        for old in base.glob(f"{code} *"):   # клиента переименовали — переименуем и папку
            if old.is_dir():
                try:
                    old.rename(want)
                except OSError:
                    return old
                break
    return want


# ---------- Сбор данных ----------
def _val(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _row(obj) -> dict:
    return {col.key: _val(getattr(obj, col.key, None)) for col in obj.__table__.columns}


def collect(db: Session, c: Client, audit_by_rec: dict | None = None) -> dict:
    """Все строки базы по клиенту (для данные.json)."""
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.client_id == c.id))}
    orders = db.scalars(select(WorkOrder).where(WorkOrder.client_id == c.id).order_by(WorkOrder.created_at)).all()
    for v in db.scalars(select(Vehicle).where(Vehicle.id.in_([o.vehicle_id for o in orders]))):
        vehicles.setdefault(v.id, v)   # машина уже у другого владельца — нужна, чтобы наряд восстановился
    ids = [o.id for o in orders]
    tables = {
        "clients": [_row(c)],
        "client_phones": [_row(p) for p in db.scalars(select(ClientPhone).where(ClientPhone.client_id == c.id)
                                                       .order_by(ClientPhone.created_at))],
        "vehicles": [_row(v) for v in sorted(vehicles.values(), key=lambda v: v.created_at)],
        "work_orders": [_row(o) for o in orders],
    }
    for m in ORDER_CHILDREN:
        tables[m.__tablename__] = [_row(r) for r in db.scalars(select(m).where(m.work_order_id.in_(ids))
                                                               .order_by(m.created_at))] if ids else []
    tables["client_events"] = [_row(e) for e in db.scalars(select(ClientEvent).where(ClientEvent.client_id == c.id)
                                                           .order_by(ClientEvent.created_at))]
    rec_ids = {r["id"] for t in AUDIT_TABLES for r in tables.get(t, [])}
    if audit_by_rec is None:
        audit_by_rec = _audit_index(db, c.branch_id)
    audit = [a for rid in rec_ids for a in audit_by_rec.get(rid, [])]
    tables["audit_log"] = [_row(a) for a in sorted(audit, key=lambda a: a.created_at)]
    uids = {r.get(k) for rows in tables.values() for r in rows for k in r
            if k in ("created_by", "manager_id", "mechanic_id", "partsman_id") and r.get(k)}
    people = {str(u.id): u.full_name for u in db.scalars(select(User).where(User.id.in_([uuid.UUID(i) for i in uids])))} \
        if uids else {}
    return {"format": FORMAT, "version": VERSION, "client_code": c.code, "client_name": c.full_name,
            "tables": tables, "people": people}


def _audit_index(db: Session, branch_id) -> dict:
    out: dict = {}
    for a in db.scalars(select(AuditLog).where(AuditLog.branch_id == branch_id, AuditLog.table.in_(list(AUDIT_TABLES)))):
        out.setdefault(a.record_id, []).append(a)
    return out


# ---------- Excel ----------
def _dt(s, fmt="%d.%m.%Y"):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).astimezone().strftime(fmt)
    except ValueError:
        return s


def _num(s):
    try:
        return float(Decimal(s)) if s not in (None, "") else None
    except Exception:
        return None


def write_xlsx(db: Session, data: dict, path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from . import money as msvc
    from . import order_feed

    t = data["tables"]
    people = data.get("people", {})
    who = lambda i: people.get(i, "") if i else ""   # noqa: E731
    veh = {v["id"]: v for v in t["vehicles"]}
    num = {o["id"]: o["number"] for o in t["work_orders"]}
    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="48484A")

    def sheet(title, cols, rows, widths, money_cols=()):
        ws = wb.create_sheet(title)
        ws.append(cols)
        for cell in ws[1]:
            cell.font, cell.fill = head, fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for ci in money_cols:
            for row in ws.iter_rows(min_row=2, min_col=ci, max_col=ci):
                for cell in row:
                    cell.number_format = "#,##0.00"
        ws.freeze_panes = "A2"
        return ws

    c = t["clients"][0]
    ws = wb.active
    ws.title = "Карточка"
    phones = ", ".join(p["phone"] for p in t["client_phones"]) or c.get("phone", "")
    notes = [e for e in t["client_events"] if e["kind"] == "note"]
    for k, v in [("Номер клиента", data.get("client_code") or ""), ("Имя", c["full_name"]), ("Телефоны", phones),
                 ("С нами с", _dt(c["created_at"])), ("Визитов (нарядов)", len(t["work_orders"])),
                 ("Машин", len([v for v in t["vehicles"] if v["client_id"] == c["id"]])),
                 ("Выгружено", datetime.now().strftime("%d.%m.%Y %H:%M")), ("", ""),
                 ("Заметки", "\n".join(f'{_dt(e["created_at"])}: {e["text"]}' for e in notes) or "—"), ("", ""),
                 ("Важно", "Этот файл — для чтения. Восстановление CRM — по файлу «данные.json» рядом, его не править.")]:
        ws.append([k, v])
    for cell in ws["A"]:
        cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90
    for row in ws.iter_rows():
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    sheet("Машины", ["Марка", "Модель", "Год", "Госномер", "VIN", "Пробег, км", "Владелец сейчас"],
          [[v["make"], v["model"], v["year"], v["plate"], v["vin"], v["mileage"],
            "этот клиент" if v["client_id"] == c["id"] else "другой владелец"] for v in t["vehicles"]],
          [14, 16, 7, 14, 22, 12, 18])

    orders_rows, works_rows, parts_rows, pay_rows, find_rows, feed_rows = [], [], [], [], [], []
    for o in sorted(t["work_orders"], key=lambda o: o["created_at"], reverse=True):
        v = veh.get(o["vehicle_id"], {})
        wo = db.get(WorkOrder, uuid.UUID(o["id"]))
        tt = msvc.totals(db, wo) if wo is not None else None
        orders_rows.append([_dt(o["created_at"]), o["number"], f'{v.get("make", "")} {v.get("model", "")}'.strip(),
                            v.get("plate", ""), o.get("mileage_in"), o.get("complaint", ""),
                            WO_STATUSES.get(o["status"], o["status"]), who(o.get("manager_id")), who(o.get("mechanic_id")),
                            float(tt.works) if tt else None, float(tt.parts) if tt else None,
                            float(tt.total) if tt else None, float(tt.paid) if tt else None, _dt(o.get("closed_at"))])
        if wo is not None:
            for e in order_feed.build(db, wo, limit=500):
                feed_rows.append([o["number"], e["at"].astimezone().strftime("%d.%m.%Y %H:%M"), e["who"],
                                  e["text"] + (f' — {e["detail"]}' if e["detail"] else "")])
    for w in t["wo_works"]:
        works_rows.append([num.get(w["work_order_id"], ""), _dt(w["created_at"]), w["name"], who(w.get("mechanic_id")),
                           _num(w.get("qty")), _num(w.get("price")), "да" if w.get("status") == "done" else "",
                           _dt(w.get("done_at"), "%d.%m.%Y %H:%M")])
    for p in t["wo_parts"]:
        if p.get("status") in PART_OFF:
            continue
        q, pr = _num(p.get("qty")) or 0, _num(p.get("price")) or 0
        parts_rows.append([num.get(p["work_order_id"], ""), p["name"], q, pr, round(q * pr, 2),
                           PART_STATUSES.get(p.get("status"), p.get("status") or "")])
    for p in t["payments"]:
        pay_rows.append([num.get(p["work_order_id"], ""), _dt(p["created_at"], "%d.%m.%Y %H:%M"), _num(p.get("amount")),
                         PAY_METHODS.get(p.get("method"), p.get("method") or ""),
                         "возврат" if p.get("purpose") == "refund" else ""])
    for f in t["wo_findings"]:
        find_rows.append([num.get(f["work_order_id"], ""), _dt(f["created_at"]), "Проблема", f["text"],
                          URGENCY.get(f.get("urgency"), "")])
    for e in t["client_events"]:
        if e["kind"] in ("recommendation", "part_refused"):
            find_rows.append([num.get(e.get("work_order_id"), ""), _dt(e["created_at"]),
                              "Рекомендация" if e["kind"] == "recommendation" else "Отказался от запчасти", e["text"],
                              URGENCY.get(e.get("urgency"), "")])

    sheet("Визиты", ["Дата", "№ наряда", "Машина", "Госномер", "Пробег", "Жалоба", "Статус", "Менеджер", "Мастер",
                     "Работы, ₴", "Запчасти, ₴", "Итого, ₴", "Оплачено, ₴", "Выдан"],
          orders_rows, [11, 18, 18, 12, 9, 40, 16, 20, 20, 12, 12, 12, 12, 11], money_cols=(10, 11, 12, 13))
    sheet("Работы", ["№ наряда", "Дата", "Работа", "Исполнитель", "Кол-во", "Сумма, ₴", "Выполнено", "Когда"],
          works_rows, [18, 11, 45, 20, 8, 12, 10, 16], money_cols=(6,))
    sheet("Запчасти", ["№ наряда", "Название", "Кол-во", "Цена, ₴", "Сумма, ₴", "Статус"],
          parts_rows, [18, 45, 8, 12, 12, 12], money_cols=(4, 5))
    sheet("Оплаты", ["№ наряда", "Когда", "Сумма, ₴", "Способ", "Примечание"], pay_rows, [18, 16, 12, 14, 14],
          money_cols=(3,))
    sheet("Проблемы и рекомендации", ["№ наряда", "Дата", "Вид", "Текст", "Срочность"], find_rows, [18, 11, 20, 60, 14])
    sheet("История нарядов", ["№ наряда", "Когда", "Кто", "Что сделано"], feed_rows, [18, 16, 22, 80])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)


# ---------- Выгрузка ----------
def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)


def export_client(db: Session, c: Client, base: Path, audit_idx: dict | None = None, force: bool = False) -> bool:
    """Записать папку клиента. True — файлы обновлены (что-то изменилось)."""
    data = collect(db, c, audit_idx)
    folder = _client_folder(base, c)
    jf = folder / JSON_NAME
    body = _dump(data)
    if not force and jf.exists() and (folder / XLSX_NAME).exists():
        try:
            old = json.loads(jf.read_text(encoding="utf-8"))
            old.pop("exported_at", None)
            if _dump(old) == body:
                return False
        except (OSError, ValueError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    data["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = jf.with_suffix(".tmp")
    tmp.write_text(_dump(data), encoding="utf-8")
    os.replace(tmp, jf)
    write_xlsx(db, data, folder / XLSX_NAME)
    # закреплённые фото машин
    from .orders import PHOTO_DIR
    pics = folder / "фото машин"
    for v in data["tables"]["vehicles"]:
        if v.get("photo") and v["client_id"] == str(c.id) and (PHOTO_DIR / v["photo"]).exists():
            pics.mkdir(exist_ok=True)
            shutil.copyfile(PHOTO_DIR / v["photo"],
                            pics / _safe(f'{v["make"]} {v["model"]} {v["plate"]}{Path(v["photo"]).suffix}'))
    return True


def export_all(db: Session, branch_id, force: bool = False) -> dict:
    from ..db import current_branch_id
    tok = current_branch_id.set(branch_id)   # ночью запроса нет — журнал берёт филиал отсюда
    try:
        return _export_all(db, branch_id, force)
    finally:
        current_branch_id.reset(tok)


def _export_all(db: Session, branch_id, force: bool = False) -> dict:
    base = root(db, branch_id)
    folder = base / "clients"
    folder.mkdir(parents=True, exist_ok=True)
    (base / "КАК ВОССТАНОВИТЬ.txt").write_text(HOWTO, encoding="utf-8")
    idx = _audit_index(db, branch_id)
    clients = db.scalars(select(Client).where(Client.branch_id == branch_id).order_by(Client.number)).all()
    changed = 0
    for c in clients:
        if export_client(db, c, folder, idx, force):
            changed += 1
    _write_index(db, branch_id, clients, base)
    st.put(db, branch_id, "client_archive", {"last_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                                              "last_clients": len(clients), "last_changed": changed})
    db.commit()
    return {"clients": len(clients), "changed": changed, "folder": str(base)}


def _write_index(db: Session, branch_id, clients, base: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Клиенты"
    ws.append(["Номер", "Имя", "Телефон", "Машины", "Нарядов", "Последний визит", "Папка"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for c in clients:
        vs = db.scalars(select(Vehicle).where(Vehicle.client_id == c.id)).all()
        os_ = db.scalars(select(WorkOrder.created_at).where(WorkOrder.client_id == c.id)).all()
        ws.append([c.code, c.full_name, c.phone, ", ".join(f"{v.make} {v.model} {v.plate}".strip() for v in vs), len(os_),
                   max(os_).astimezone().strftime("%d.%m.%Y") if os_ else "", _client_folder(base / "clients", c).name])
    for col, w in zip("ABCDEFG", (10, 30, 16, 40, 9, 15, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    tmp = base / "_Список клиентов.tmp.xlsx"
    wb.save(tmp)
    os.replace(tmp, base / "_Список клиентов.xlsx")


def make_zip(db: Session, branch_id) -> Path:
    base = root(db, branch_id)
    out = DATA_DIR / "archive-download.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if base.exists():
            for f in base.rglob("*"):
                if f.is_file() and f.resolve() != out.resolve():
                    z.write(f, f.relative_to(base))
    return out


def status(db: Session, branch_id) -> dict:
    s = st.get(db, branch_id, "client_archive")
    base = root(db, branch_id)
    n = len(list((base / "clients").glob(f"*/{JSON_NAME}"))) if (base / "clients").exists() else 0
    size = sum(f.stat().st_size for f in base.rglob("*") if f.is_file()) if base.exists() else 0
    return {**s, "folder_path": str(base), "n_folders": n, "size": size, "custom": bool((s.get("folder") or "").strip())}


# ---------- Восстановление ----------
def read_docs(folder: Path) -> tuple[list[dict], list[str]]:
    docs, errors = [], []
    if not folder.exists():
        return [], [f"Папка не найдена: {folder}"]
    for f in sorted(folder.rglob(JSON_NAME)):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append(f"Не читается: {f.parent.name}")
            continue
        if d.get("format") != FORMAT:
            errors.append(f"Не архив CRM: {f.parent.name}")
            continue
        docs.append(d)
    return docs, errors


def _impl(t):
    if isinstance(t, TypeDecorator):
        impl = t.impl
        return impl() if isinstance(impl, type) else impl
    return t


def _py(col, v):
    if v is None:
        return None
    t = _impl(col.type)
    try:
        if isinstance(t, T.Uuid):
            return uuid.UUID(str(v))
        if isinstance(t, T.DateTime):
            d = datetime.fromisoformat(v)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        if isinstance(t, T.Date):
            return date.fromisoformat(v)
        if isinstance(t, T.Numeric):
            return Decimal(str(v))
        if isinstance(t, T.Boolean):
            return bool(v)
        if isinstance(t, T.Integer):
            return int(v)
    except (ValueError, TypeError, ArithmeticError):
        return None
    return v


def _existing(db: Session, table, ids: set) -> set:
    out = set()
    ids = list(ids)
    for i in range(0, len(ids), 500):
        out |= set(db.scalars(select(table.c.id).where(table.c.id.in_(ids[i:i + 500]))))
    return out


def restore(db: Session, docs: list[dict], branch_id, user_id, dry: bool = False) -> dict:
    """Добавить из архива всё, чего нет в базе (по id). dry — только посчитать."""
    stats = {m.__tablename__: 0 for m in MODELS}
    skipped = 0
    known: dict[str, set] = {}   # таблица → id, которые есть в базе или добавлены сейчас

    def has(table, value) -> bool:
        s = known.setdefault(table.name, set())
        if value in s:
            return True
        if db.scalar(select(table.c.id).where(table.c.id == value)) is not None:
            s.add(value)
            return True
        return False

    for m in MODELS:
        table = m.__table__
        cols = {c.key: c for c in table.columns}
        rows: dict = {}
        for d in docs:
            for r in d.get("tables", {}).get(table.name, []):
                if r.get("id"):
                    rows.setdefault(r["id"], r)
        if not rows:
            continue
        present = _existing(db, table, {uuid.UUID(i) for i in rows})
        known.setdefault(table.name, set()).update(present)
        for rid, r in rows.items():
            u = uuid.UUID(rid)
            if u in present:
                continue
            vals = {k: _py(cols[k], v) for k, v in r.items() if k in cols}
            vals["branch_id"] = branch_id
            ok = True
            for col in table.columns:
                if col.key in ("id", "branch_id") or not col.foreign_keys:
                    continue
                v = vals.get(col.key)
                if v is None:
                    continue
                target = next(iter(col.foreign_keys)).column.table
                if has(target, v):
                    continue
                if col.nullable:
                    vals[col.key] = None
                elif target.name == "users":
                    vals[col.key] = user_id
                else:
                    ok = False
                    break
            if not ok:
                skipped += 1
                continue
            if dry:
                known[table.name].add(u)
                stats[table.name] += 1
                continue
            sp = db.begin_nested()
            try:
                db.execute(table.insert().values(**vals))
                sp.commit()
                known[table.name].add(u)
                stats[table.name] += 1
            except Exception:
                sp.rollback()
                skipped += 1
    if not dry:
        from ..audit import write
        write(db, "archive", "-", "restore", {k: v for k, v in stats.items() if v} | {"skipped": skipped})
        db.commit()
    return {"stats": stats, "skipped": skipped, "docs": len(docs)}
