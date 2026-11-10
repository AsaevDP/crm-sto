"""Настройки филиала: значения по умолчанию + чтение/запись."""
import copy
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS = {
    "branding": {
        "name": "СТО №1",          # название в шапке
        "accent": "blue",          # цвет экрана входа (у ролей — свои)
        "logo": "",                # путь к файлу в media/branding
        "icon": "",                # иконка приложения (PWA, шаг 8)
    },
    "order_number": {
        # Метки: {код} — код филиала, {год} — 2026, {гг} — 26, {номер:6} — 000123 (число = сколько цифр)
        "template": "{код}-{год}-{номер:6}",
        "reset_yearly": True,
    },
    "pricing": {
        "hour_rate": 800,          # цена нормо-часа по умолчанию, ₴ (в каждой работе меняется вручную)
        "default_markup": 25,      # наценка на запчасти по умолчанию, %; в наряде вписывается вручную по каждой позиции (0–1000)
    },
    "payroll": {
        "partsman_margin_pct": 7,
        "mechanic_works_pct": 30,  # % слесарю от его работ по умолчанию (у каждого можно своё)  # % запчастисту от НАЦЕНКИ (не от цены). Меняет только главный админ, 0–30
    },
    "schedule": {
        "day_start": 8,            # с какого часа сетка записи
        "day_end": 19,             # до какого часа
        "step": 30,                # шаг сетки, минут
        "days_off": [6],           # выходные (0 — пн … 6 — вс): для загрузки мастеров за неделю/месяц
    },
    "session": {
        "idle_minutes": 60,        # автовыход без действий (для ПК со входом по карте)
    },
    # Наклейки со штрихкодом (10.11.2026). Оборудования пока нет: размер и режим настраиваются тут,
    # сам принтер выбирается в окне печати браузера.
    "labels": {
        "width_mm": 100, "height_mm": 70,
        "mode": "label",           # label — принтер наклеек (одна наклейка = одна страница), sheet — лист A4
        "printer": "",             # название принтера — для памяти (выбирается в окне печати)
        "show_price": True, "show_cell": True, "show_fits": True,
    },
    # Шаблон отчётных документов Кабинета: подставляется в каждый документ, правится прямо на листе
    "report_doc": {
        "header": "",              # реквизиты: ФОП / ООО, адрес, телефон, ЕДРПОУ / ИНН — каждая строка с новой строки
        "footer": "Отчёт сформирован в CRM СТО.",
        "signer1": "Директор",
        "signer2": "Бухгалтер",
    },
}


def get(db: Session, branch_id: uuid.UUID, key: str) -> dict:
    row = db.scalar(select(Setting).where(Setting.branch_id == branch_id, Setting.key == key))
    value = copy.deepcopy(DEFAULTS.get(key, {}))
    if row:
        value.update(row.value or {})
    return value


def put(db: Session, branch_id: uuid.UUID, key: str, value: dict):
    row = db.scalar(select(Setting).where(Setting.branch_id == branch_id, Setting.key == key))
    if row:
        row.value = {**(row.value or {}), **value}  # новый dict — чтобы SQLAlchemy увидел изменение
    else:
        db.add(Setting(branch_id=branch_id, key=key, value=value))
