"""Номер наряда по шаблону из настроек. По умолчанию: СТО1-2026-000123."""
import re
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Branch, OrderCounter
from . import settings as st

_NUM = re.compile(r"\{номер(?::(\d+))?\}")


def format_number(template: str, code: str, year: int, seq: int) -> str:
    out = template.replace("{код}", code).replace("{год}", str(year)).replace("{гг}", f"{year % 100:02d}")
    return _NUM.sub(lambda m: str(seq).zfill(int(m.group(1) or 1)), out)


def preview(template: str, code: str) -> str:
    return format_number(template, code, date.today().year, 123)


def next_number(db: Session, branch_id: uuid.UUID) -> str:
    """Выдать следующий номер. Вызывать внутри транзакции создания наряда."""
    cfg = st.get(db, branch_id, "order_number")
    branch = db.get(Branch, branch_id)
    year = date.today().year if cfg.get("reset_yearly", True) else 0
    counter = db.scalar(select(OrderCounter).where(OrderCounter.branch_id == branch_id, OrderCounter.year == year))
    if counter is None:
        counter = OrderCounter(branch_id=branch_id, year=year, last=0)
        db.add(counter)
    counter.last += 1
    return format_number(cfg["template"], branch.code, date.today().year, counter.last)


# ---------------------------------------------------------------- поиск по номеру наряда (25.11.2026, брат)
# Вводят не весь номер «СТО1-2026-000039», а коротко. По каким частям искать — «Настройки → Номер наряда»:
#   seq  — номер по порядку (всегда): 39 или 000039;
#   year — год: «39» = наряд этого года, прошлого — «25-39» или «2025-39» (если нумерация каждый год с начала);
#   code — код СТО: «СТО1-39» (если филиалов несколько).
SEARCH_DEFAULT = {"year": True, "code": False}


def search_cfg(db: Session, branch_id) -> dict:
    cfg = st.get(db, branch_id, "order_number") or {}
    out = dict(SEARCH_DEFAULT)
    out.update({k: bool(v) for k, v in (cfg.get("search") or {}).items() if k in SEARCH_DEFAULT})
    out["yearly"] = bool(cfg.get("reset_yearly", True))
    return out


def parts_of(number: str) -> dict:
    """Из готового номера: seq — последняя группа цифр, year — 4 цифры 19xx/20xx или 2 цифры перед номером, code — начало."""
    groups = re.findall(r"\d+", number or "")
    seq = int(groups[-1]) if groups else None
    year = next((int(g) for g in groups[:-1] if len(g) == 4 and g[:2] in ("19", "20")), None)
    if year is None:
        two = [g for g in groups[:-1] if len(g) == 2]
        year = 2000 + int(two[-1]) if two else None
    code = re.split(r"[-/ ]", number or "")[0] if number else ""
    return {"seq": seq, "year": year, "code": code.upper()}


def hint(db: Session, branch_id) -> str:
    c = search_cfg(db, branch_id)
    from datetime import date as _d
    y = _d.today().year
    s = "Поиск наряда: «39» или «000039»"
    if c["year"] and c["yearly"]:
        s += f" — наряд {y} года; прошлого года — «{(y - 1) % 100}-39» или «{y - 1}-39»"
    if c["code"]:
        s += "; с кодом СТО — «СТО1-39»"
    return s


def find_orders(db: Session, branch_id, q: str, limit: int = 20) -> list:
    """Наряды по короткому номеру (или полному). Пусто — если это не похоже на номер наряда."""
    from ..models import WorkOrder
    q = (q or "").strip()
    if not q:
        return []
    full = db.scalar(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.number == q.upper()))
    if full:
        return [full]
    m = re.fullmatch(r"(?:([^\d\s][^\s/-]*)[\s/-]+)?(?:(\d{2}|\d{4})[\s/-]+)?0*(\d{1,7})", q)
    if not m:
        return []
    code, year, seq = (m.group(1) or "").upper(), m.group(2), int(m.group(3))
    c = search_cfg(db, branch_id)
    want_year = None
    if c["year"] and c["yearly"]:
        from datetime import date as _d
        want_year = (2000 + int(year) if year and len(year) == 2 else int(year)) if year else _d.today().year
    elif year:
        want_year = 2000 + int(year) if len(year) == 2 else int(year)
    rows = db.scalars(select(WorkOrder).where(WorkOrder.branch_id == branch_id, WorkOrder.number.like(f"%{seq}"))
                      .order_by(WorkOrder.created_at.desc())).all()
    out = []
    for o in rows:
        p = parts_of(o.number)
        if p["seq"] != seq:
            continue
        if want_year and (p["year"] or o.created_at.year) != want_year:
            continue
        if code and c["code"] and p["code"] != code:
            continue
        out.append(o)
    return out[:limit]