"""Лента действий наряда (11.11.2026): кто, что и когда менял — коротко, человеческими словами.

Берётся из журнала (audit_log): сам наряд, его запчасти, работы, проблемы, оплаты, фото, запросы на выдачу.
Подряд одинаковые действия одного человека (например, 3 фото) склеиваются в одну строку.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, IssueRequest, Payment, User, WoFinding, WoPart, WoPhoto, WorkOrder, WoWork
from ..models.money import PAY_METHODS
from ..models.orders import PART_STATUSES, WO_STATUSES

CHILDREN = {"wo_parts": WoPart, "wo_works": WoWork, "wo_findings": WoFinding, "payments": Payment,
            "wo_photos": WoPhoto, "issue_requests": IssueRequest}
WORK_STATUS = {"todo": "ждёт", "doing": "в работе", "paused": "пауза", "done": "готово"}


def _ago(at: datetime) -> str:
    now = datetime.now(timezone.utc)
    d = now - at
    if d < timedelta(minutes=1):
        return "только что"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} мин назад"
    loc = at.astimezone()
    if loc.date() == now.astimezone().date():
        return f"сегодня {loc:%H:%M}"
    if loc.date() == (now.astimezone() - timedelta(days=1)).date():
        return f"вчера {loc:%H:%M}"
    return f"{loc:%d.%m %H:%M}"


def _name(ch: dict, obj, key="name") -> str:
    v = (ch or {}).get(key) or (getattr(obj, key, "") if obj is not None else "")
    v = str(v or "")
    return v if len(v) <= 60 else v[:57] + "…"


def _pair(ch: dict, k):
    v = (ch or {}).get(k)
    if isinstance(v, list) and len(v) == 2:
        return v[0], v[1]
    return None, v


def _sentence(r: AuditLog, obj, people: dict) -> tuple[str, str]:
    """(что сделал, подробность) для одной записи журнала."""
    ch, a, t = r.changes or {}, r.action, r.table

    def person(v):
        try:
            u = people.get(uuid.UUID(str(v)))
        except ValueError:
            u = None
        return u.short_name if u else "—"

    if t == "work_orders":
        if a == "create":
            return "принял машину — наряд создан", ""
        parts = []
        if "status" in ch:
            old, new = _pair(ch, "status")
            parts.append(f"этап: {WO_STATUSES.get(old, old or '—')} → {WO_STATUSES.get(new, new)}")
        for k, lbl in (("manager_id", "менеджер"), ("mechanic_id", "слесарь"), ("partsman_id", "запчастист")):
            if k in ch:
                old, new = _pair(ch, k)
                parts.append(f"{lbl}: {person(old) if old else '—'} → {person(new) if new else '—'}")
        if "lift_id" in ch:
            parts.append("сменил пост")
        if "discount_pct" in ch:
            parts.append(f"скидка {_pair(ch, 'discount_pct')[1]} %")
        if not parts:
            return "изменил наряд", ""
        if "manager_id" in ch and len(parts) == 1:
            return "передал наряд другому менеджеру", parts[0]
        return parts[0][:1].upper() + parts[0][1:], "; ".join(parts[1:])
    if t == "wo_parts":
        nm = _name(ch, obj)
        if a == "create":
            st = ch.get("status")
            return (f"предложил вариант «{nm}»" if st == "option" else f"добавил запчасть «{nm}»"), ""
        if a == "delete":
            return f"убрал запчасть «{nm}»", ""
        if "urgent" in ch:
            return (f"пометил «срочно»: {nm}" if _pair(ch, "urgent")[1] else f"снял «срочно»: {nm}"), ""
        if "status" in ch:
            old, new = _pair(ch, "status")
            if new == "declined":
                return f"вариант не выбран: {nm}", ""
            if old == "option":
                return f"клиент выбрал: {nm}", ""
            return f"«{nm}»: {PART_STATUSES.get(old, old or '—')} → {PART_STATUSES.get(new, new)}", ""
        if "price" in ch or "cost" in ch or "markup_pct" in ch:
            return f"изменил цену: {nm}", ""
        return f"изменил запчасть «{nm}»", ""
    if t == "wo_works":
        nm = _name(ch, obj)
        if a == "create":
            return f"добавил работу «{nm}»", ""
        if a == "delete":
            return f"убрал работу «{nm}»", ""
        if "status" in ch:
            new = _pair(ch, "status")[1]
            return (f"выполнил работу «{nm}»" if new == "done" else f"работа «{nm}»: {WORK_STATUS.get(new, new)}"), ""
        return f"изменил работу «{nm}»", ""
    if t == "wo_findings":
        nm = _name(ch, obj, "text")
        if a == "create":
            return f"записал проблему «{nm}»", ""
        if a == "delete":
            return f"удалил проблему «{nm}»", ""
        if "approval" in ch:
            v = _pair(ch, "approval")[1]
            return {"yes": f"клиент согласен: {nm}", "no": f"клиент отказался: {nm}"}.get(v, f"согласование: {nm}"), ""
        return f"изменил проблему «{nm}»", ""
    if t == "payments" and a == "create":
        amt = ch.get("amount") or (obj.amount if obj is not None else "")
        try:
            amt = f"{float(amt):,.2f}".replace(",", " ").replace(".00", "")
        except (TypeError, ValueError):
            pass
        return f"принял оплату {amt} ₴", PAY_METHODS.get(ch.get("method") or getattr(obj, "method", ""), "")
    if t == "wo_photos" and a == "create":
        return "добавил фото", ""
    if t == "issue_requests":
        if a == "create":
            return "запросил выдачу запчастей", ""
        if _pair(ch, "status")[1] == "ready":
            return "подготовил запчасти — готово к выдаче", ""
        if _pair(ch, "status")[1] == "issued":
            if _pair(ch, "status")[0] == "ready":
                return "отметил «Выдано» — подготовленные запчасти забрали", ""
            return "выдал запчасти со склада", ""
    return "", ""


def build(db: Session, wo: WorkOrder, limit: int = 200) -> list[dict]:
    ids = {"work_orders": {str(wo.id)}}
    for table, model in CHILDREN.items():
        ids[table] = {str(i) for i in db.scalars(select(model.id).where(model.work_order_id == wo.id))}
    rows = []
    for table, rid in ids.items():
        if rid:
            rows += db.scalars(select(AuditLog).where(AuditLog.table == table, AuditLog.record_id.in_(list(rid)))).all()
    # Удалённое (запчасть убрали) — по снимку в журнале
    for r in db.scalars(select(AuditLog).where(AuditLog.table.in_(list(CHILDREN)), AuditLog.action == "delete",
                                               AuditLog.created_at >= wo.created_at)):
        if (r.changes or {}).get("work_order_id") == str(wo.id):
            rows.append(r)
    rows.sort(key=lambda r: r.created_at, reverse=True)
    objs = {}
    for table, model in CHILDREN.items():
        if ids[table]:
            for o in db.scalars(select(model).where(model.id.in_([uuid.UUID(i) for i in ids[table]]))):
                objs[(table, str(o.id))] = o
    people = {u.id: u for u in db.scalars(select(User))}
    from ..web import color_css
    out: list[dict] = []
    for r in rows:
        text, detail = _sentence(r, objs.get((r.table, r.record_id)), people)
        if not text:
            continue
        u = people.get(r.created_by)
        e = {"who_id": r.created_by, "who_short": u.short_name if u else "Система", "who": u.full_name if u else "Система",
             "color": color_css(u.role.color) if u else "var(--gray)", "at": r.created_at, "when": _ago(r.created_at),
             "text": text, "detail": detail, "n": 1}
        prev = out[-1] if out else None
        # Подряд то же действие того же человека в пределах 3 минут — одной строкой (×N)
        if prev and prev["who_id"] == e["who_id"] and prev["base"] == text and prev["at"] - r.created_at < timedelta(minutes=3):
            prev["n"] += 1
            prev["text"] = f"{text} ×{prev['n']}"
            continue
        e["base"] = text
        out.append(e)
        if len(out) >= limit:
            break
    return out
