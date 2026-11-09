"""Единая панель дат (10.11.2026): день / неделя / месяц / квартал / год / свой период / всё время.
Везде одинаковые параметры адреса: p (вид), d (опорная дата), f и t (свой период).
Клавиши (app.js): ← → — назад / вперёд, Т — сегодня, Д Н М К Г — текущие день, неделя, месяц, квартал, год."""
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

KINDS = {"day": "День", "week": "Неделя", "month": "Месяц", "quarter": "Квартал", "year": "Год",
         "custom": "Свой период", "all": "Всё время"}
KEYS = {"day": "Д", "week": "Н", "month": "М", "quarter": "К", "year": "Г"}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
              "декабря"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _month_end(y: int, m: int) -> date:
    return date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)


def bounds(kind: str, a: date) -> tuple[date, date]:
    if kind == "day":
        return a, a
    if kind == "week":
        s = a - timedelta(days=a.weekday())
        return s, s + timedelta(days=6)
    if kind == "quarter":
        q = (a.month - 1) // 3
        return date(a.year, 3 * q + 1, 1), _month_end(a.year, 3 * q + 3)
    if kind == "year":
        return date(a.year, 1, 1), date(a.year, 12, 31)
    return a.replace(day=1), _month_end(a.year, a.month)


def shift(kind: str, a: date, n: int) -> date:
    if kind == "day":
        return a + timedelta(days=n)
    if kind == "week":
        return a + timedelta(days=7 * n)
    months = {"month": 1, "quarter": 3, "year": 12}.get(kind, 1) * n
    m = a.month - 1 + months
    y, m = a.year + m // 12, m % 12 + 1
    return date(y, m, min(a.day, _month_end(y, m).day))


def label(kind: str, d1: date, d2: date) -> str:
    if kind == "day":
        return f"{WEEKDAYS[d1.weekday()]}, {d1.day} {MONTHS_GEN[d1.month - 1]} {d1.year}"
    if kind == "week":
        if d1.month == d2.month:
            return f"{d1.day}–{d2.day} {MONTHS_GEN[d2.month - 1]} {d2.year}"
        return f"{d1.day} {MONTHS_GEN[d1.month - 1]} – {d2.day} {MONTHS_GEN[d2.month - 1]} {d2.year}"
    if kind == "month":
        return f"{MONTHS[d1.month - 1].capitalize()} {d1.year}"
    if kind == "quarter":
        return f"{(d1.month - 1) // 3 + 1} квартал {d1.year}"
    if kind == "year":
        return f"{d1.year} год"
    if kind == "all":
        return "Всё время"
    return f"{d1:%d.%m.%Y} — {d2:%d.%m.%Y}"


@dataclass
class Period:
    kind: str
    d1: date | None
    d2: date | None
    anchor: date
    allowed: tuple = ()
    label: str = ""

    def params(self, kind: str | None = None, d: date | None = None) -> dict:
        k = kind or self.kind
        if k == "custom":
            return {"p": "custom", "f": self.d1.isoformat(), "t": self.d2.isoformat()}
        if k == "all":
            return {"p": "all"}
        return {"p": k, "d": (d or self.anchor).isoformat()}

    @property
    def qs(self) -> str:
        return urlencode(self.params())

    @property
    def is_current(self) -> bool:
        return self.kind not in ("custom", "all") and self.d1 <= date.today() <= self.d2


def parse(p: str = "", d: str = "", f: str = "", t: str = "", default: str = "month",
          allowed: tuple = ("day", "week", "month", "quarter", "year", "custom")) -> Period:
    today = date.today()
    kind = p if p in allowed else default
    if kind == "all":
        return Period("all", None, None, today, allowed, label("all", today, today))
    if kind == "custom":
        try:
            a, b = date.fromisoformat(f), date.fromisoformat(t)
        except ValueError:
            kind = default if default != "custom" else "month"
        else:
            a, b = min(a, b), max(a, b)
            return Period("custom", a, b, a, allowed, label("custom", a, b))
    try:
        anchor = date.fromisoformat(d) if d else today
    except ValueError:
        anchor = today
    d1, d2 = bounds(kind, anchor)
    return Period(kind, d1, d2, anchor, allowed, label(kind, d1, d2))


@dataclass
class Bar:
    """Готовые ссылки для панели дат (шаблон _period.html)."""
    per: Period
    path: str
    keep: dict = field(default_factory=dict)

    def url(self, **params) -> str:
        q = {k: v for k, v in {**self.keep, **params}.items() if v not in ("", None)}
        return f"{self.path}?{urlencode(q)}" if q else self.path

    @property
    def prev(self) -> str:
        if self.per.kind in ("custom", "all"):
            return ""
        return self.url(**self.per.params(d=shift(self.per.kind, self.per.anchor, -1)))

    @property
    def next(self) -> str:
        if self.per.kind in ("custom", "all"):
            return ""
        return self.url(**self.per.params(d=shift(self.per.kind, self.per.anchor, 1)))

    @property
    def today(self) -> str:
        k = self.per.kind if self.per.kind not in ("custom", "all") else next(
            (x for x in ("month", "week", "day", "year") if x in self.per.allowed), "month")
        return self.url(p=k, d=date.today().isoformat())

    def quick(self) -> list[tuple[str, str, str, bool]]:
        """[(вид, подпись, ссылка, выбран)] — быстрые кнопки на текущий период."""
        out = []
        for k in ("day", "week", "month", "quarter", "year", "all"):
            if k in self.per.allowed:
                href = self.url(p="all") if k == "all" else self.url(p=k, d=date.today().isoformat())
                out.append((k, KINDS[k], href, self.per.kind == k))
        return out
