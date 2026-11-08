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
