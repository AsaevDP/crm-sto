"""Инструмент: выдача/возврат, проверки (приём, увольнение, плановая), удержания из зарплаты."""
import secrets
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PayrollAccrual, Tool, ToolCheck, ToolCheckLine, ToolMove, User
from ..models.tools import CHECK_KINDS, CHECK_RESULTS
from . import orders as osvc

TOOL_PHOTO_DIR = osvc.PHOTO_DIR / "tools"


def next_inv_no(db: Session, branch_id) -> str:
    n = db.scalar(select(func.count()).select_from(Tool).where(Tool.branch_id == branch_id)) or 0
    while True:
        n += 1
        code = f"И-{n:04d}"
        if not db.scalar(select(Tool.id).where(Tool.branch_id == branch_id, Tool.inv_no == code)):
            return code


def save_photo(data: bytes) -> str:
    ext = osvc._is_image(data)
    if not ext or len(data) > osvc.MAX_PHOTO_BYTES:
        return ""
    TOOL_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(8)}.{ext}"
    (TOOL_PHOTO_DIR / name).write_bytes(data)
    return f"tools/{name}"


def move(db: Session, tool: Tool, to_user: User | None, comment: str = "", wo=None, work=None):
    """Выдать сотруднику (to_user) или вернуть на склад инструмента (None).
    Общий (не личный) инструмент выдаётся под наряд wo и, по желанию, конкретную работу work."""
    wo_id = wo.id if (to_user and wo) else None
    work_id = work.id if (to_user and work) else None
    if tool.holder_id == (to_user.id if to_user else None) and tool.work_order_id == wo_id and tool.wo_work_id == work_id:
        return
    db.add(ToolMove(tool_id=tool.id, kind="issue" if to_user else "return", from_user=tool.holder_id,
                    to_user=to_user.id if to_user else None, comment=comment.strip()[:500],
                    work_order_id=wo_id or (tool.work_order_id if not to_user else None),
                    wo_work_id=work_id or (tool.wo_work_id if not to_user else None)))
    tool.holder_id = to_user.id if to_user else None
    tool.work_order_id, tool.wo_work_id = wo_id, work_id


def personal_filter():
    """Условие «личный инструмент» (у старого инструмента категории нет — он личный). Запасные наборы — тоже
    не под наряд, поэтому сюда же."""
    from sqlalchemy import or_
    return or_(Tool.category.in_(("personal", "spare")), Tool.category.is_(None), Tool.category == "")


# ---------- Шкафы и полки (11.11.2026) ----------
def shelf_label(db: Session, shelf_id) -> str:
    from ..models import ToolCabinet, ToolShelf
    sh = db.get(ToolShelf, shelf_id) if shelf_id else None
    cab = db.get(ToolCabinet, sh.cabinet_id) if sh else None
    return f"{cab.name} · {sh.name}" if sh and cab else ""


def set_shelf(db: Session, tool: Tool, shelf_id):
    """Положить инструмент на полку (или убрать с полок, shelf_id=None). Подпись места обновляется сама."""
    had = tool.shelf_id
    tool.shelf_id = shelf_id or None
    if tool.shelf_id:
        tool.place = shelf_label(db, tool.shelf_id)
    elif had:
        tool.place = ""


def adopt_text_places(db: Session, branch_id) -> int:
    """Раньше место писали текстом «Шкаф 1 · полка 2». Один раз превращаем такие подписи в шкафы и полки
    справочника (если шкафов ещё нет), чтобы экран «Шкафы» сразу показал, что где лежит."""
    from ..models import ToolCabinet, ToolShelf
    if db.scalar(select(ToolCabinet.id).where(ToolCabinet.branch_id == branch_id)):
        return 0
    tools = db.scalars(select(Tool).where(Tool.branch_id == branch_id, Tool.shelf_id.is_(None),
                                          Tool.place.is_not(None), Tool.place != "")).all()
    cabs, shelves, n = {}, {}, 0
    for t in sorted(tools, key=lambda t: t.place):
        cab_name, _, shelf_name = (t.place or "").partition("·")
        cab_name, shelf_name = cab_name.strip()[:60], shelf_name.strip()[:60]
        if not cab_name or not shelf_name:
            continue
        if cab_name not in cabs:
            cabs[cab_name] = ToolCabinet(branch_id=branch_id, name=cab_name, sort=len(cabs) + 1)
            db.add(cabs[cab_name])
            db.flush()
        key = (cab_name, shelf_name.lower())
        if key not in shelves:
            shelves[key] = ToolShelf(branch_id=branch_id, cabinet_id=cabs[cab_name].id, name=shelf_name.capitalize(),
                                     sort=int("".join(ch for ch in shelf_name if ch.isdigit()) or 0))
            db.add(shelves[key])
            db.flush()
        t.shelf_id = shelves[key].id
        t.place = f"{cab_name} · {shelves[key].name}"
        n += 1
    return n


def cabinets(db: Session, branch_id) -> list[dict]:
    """Шкафы → полки → что на полке (вместе с выданным: где он должен лежать)."""
    from ..models import ToolCabinet, ToolShelf
    cabs = db.scalars(select(ToolCabinet).where(ToolCabinet.branch_id == branch_id)
                      .order_by(ToolCabinet.sort, ToolCabinet.name)).all()
    shelves = db.scalars(select(ToolShelf).where(ToolShelf.branch_id == branch_id).order_by(ToolShelf.sort, ToolShelf.name)).all()
    tools = db.scalars(select(Tool).where(Tool.branch_id == branch_id, Tool.shelf_id.is_not(None),
                                          Tool.status != "written_off").order_by(Tool.name)).all()
    by_shelf = {}
    for t in tools:
        by_shelf.setdefault(t.shelf_id, []).append(t)
    out = []
    for c in cabs:
        sh = [{"s": s, "tools": by_shelf.get(s.id, [])} for s in shelves if s.cabinet_id == c.id]
        n = sum(len(x["tools"]) for x in sh)
        out.append({"c": c, "shelves": sh, "n": n, "out": sum(1 for x in sh for t in x["tools"] if t.holder_id)})
    return out


def for_order(db: Session, wo_id) -> list[Tool]:
    """Общий инструмент, который сейчас числится под этот наряд."""
    return db.scalars(select(Tool).where(Tool.work_order_id == wo_id, Tool.holder_id.is_not(None))
                      .order_by(Tool.inv_no)).all()


def available_shared(db: Session, branch_id) -> list[Tool]:
    """Общий инструмент на складе — можно выдать под наряд."""
    return db.scalars(select(Tool).where(Tool.branch_id == branch_id, Tool.holder_id.is_(None),
                                         Tool.status.in_(("ok", "worn")), ~personal_filter())
                      .order_by(Tool.category, Tool.name)).all()


# ---------- Запросы слесаря (11.11.2026): «Попросить инструмент» / «Сдаю» → окошко у кладовщика ----------
OPEN = ("new", "ready")   # 19.11.2026: «Готово к выдаче» — запрос ещё открыт, инструмент отложен под него


def open_request(db: Session, tool_id):
    from ..models import ToolRequest
    return db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool_id, ToolRequest.status.in_(OPEN)))


def mark_ready(db: Session, req, by: User) -> str:
    """Кладовщик достал инструмент из шкафа — «📦 Готово к выдаче» (19.11.2026, брат). Тому, кто просил, — сообщение
    и зелёная цифра у «Моих запросов»; «Выдано» — когда за ним пришли. Ошибка или ''."""
    from . import notify as nsvc
    tool = db.get(Tool, req.tool_id)
    if req.kind != "issue" or req.status != "new" or not tool:
        return "Запрос уже закрыт"
    if tool.holder_id:
        return f"«{tool.name}» сейчас на руках — сначала его должны сдать"
    req.status, req.ready_at, req.ready_by = "ready", now(), by.id
    from ..models import WorkOrder
    wo = db.get(WorkOrder, req.work_order_id) if req.work_order_id else None
    nsvc.send(db, [req.user_id], "tool_ready", "Инструмент готов к выдаче — подойдите к кладовщику",
              tool.name + (f" · {tool.place}" if tool.place else "") + (f" · наряд {wo.number}" if wo else ""),
              "/my-requests?tab=tools", wo, skip=by.id)
    return ""


def can_request_for_order(db: Session, user: User, wo) -> bool:
    """Кто может попросить общий инструмент под наряд (12.11.2026): ведущий мастер наряда и любой мастер,
    у которого в этом наряде есть своя невыполненная работа (электрик, моторист рядом со слесарем), и админ."""
    from ..models import WoWork
    from ..models.orders import WO_ACTIVE
    if user.role.code == "admin":
        return True
    if wo.status not in WO_ACTIVE:
        return False
    if user.id == wo.mechanic_id:
        return True
    return db.scalar(select(WoWork.id).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id == user.id,
                                             WoWork.status != "done").limit(1)) is not None


def request_issue(db: Session, tool: Tool, user: User, wo=None, work=None, comment: str = "") -> str:
    """Слесарь просит общий инструмент под наряд. Ошибка или ''."""
    from ..models import ToolRequest
    if tool.is_personal or tool.holder_id or tool.status not in ("ok", "worn"):
        return f"«{tool.name}» сейчас нельзя выдать — занят или неисправен"
    if open_request(db, tool.id):
        return f"«{tool.name}» уже кто-то попросил — ждёт кладовщика"
    db.add(ToolRequest(tool_id=tool.id, user_id=user.id, kind="issue", work_order_id=wo.id if wo else None,
                       wo_work_id=work.id if work else None, comment=comment.strip()[:300]))
    return ""


def request_return(db: Session, tool: Tool, user: User, comment: str = "") -> str:
    """Слесарь сдаёт общий инструмент обратно в шкаф. Кладовщик подтверждает «Принял»."""
    from ..models import ToolRequest
    if tool.is_personal or tool.holder_id != user.id:
        return "Сдать можно только общий инструмент, который числится за вами"
    if open_request(db, tool.id):
        return "Уже отмечено «Сдаю» — ждёт кладовщика"
    db.add(ToolRequest(tool_id=tool.id, user_id=user.id, kind="return", work_order_id=tool.work_order_id,
                       wo_work_id=tool.wo_work_id, comment=comment.strip()[:300]))
    return ""


def complete_request(db: Session, req, by: User, result: str = "ok", note: str = "", pickup: bool = False) -> str:
    """Кладовщик: выдал (issue) или принял (return, ok / broken с комментарием)."""
    from ..models import WorkOrder, WoWork
    tool = db.get(Tool, req.tool_id)
    who = db.get(User, req.user_id)
    if req.status not in OPEN or not tool or not who:
        return "Запрос уже закрыт"
    if req.kind == "issue":
        if tool.holder_id:
            return f"«{tool.name}» уже на руках — сначала его должны сдать"
        wo = db.get(WorkOrder, req.work_order_id) if req.work_order_id else None
        work = db.get(WoWork, req.wo_work_id) if req.wo_work_id else None
        # 19.11.2026 (брат): подготовленное может забрать у окошка менеджер и отнести мастеру — на руках у мастера
        via = f" · забрал у окошка {by.full_name}" if pickup else ""
        move(db, tool, who, f"По запросу слесаря{' · наряд ' + wo.number if wo else ''}{via}", wo=wo, work=work)
        from . import notify as nsvc   # 20.11.2026: мастер подтверждает «✓ Получил»
        nsvc.send(db, [who.id], "tool_issued", "Инструмент выдан — подтвердите «Получил»",
                  tool.name + (f" · наряд {wo.number}" if wo else ""), "/my-requests?tab=tools", wo, skip=by.id)
    else:
        move(db, tool, None, "Сдал в шкаф" + (f" · поломка: {note.strip()}" if result == "broken" else ""))
        if result == "broken":
            tool.status = "broken"
        req.result, req.result_note = ("broken" if result == "broken" else "ok"), note.strip()[:300]
    req.status, req.done_by, req.done_at = "done", by.id, now()
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.user_id == req.user_id, Notification.kind == "tool_ready",
                                                   Notification.read_at.is_(None), Notification.text.startswith(tool.name))):
        n.read_at = now()   # выдали — «готово, подойдите» по этому инструменту больше не нужно
    return ""


def return_by_keeper(db: Session, tool: Tool, by: User, broken: bool = False, note: str = "") -> str:
    """Кладовщик: «Вернул» (20.11.2026, брат) — принесли к окошку без «Сдаю» в CRM (мастер или менеджер).
    Инструмент — в шкаф. Если мастер уже нажал «Сдаю» — тот запрос закрывается; «Получил», если не нажимал, не нужен."""
    from ..models import ToolRequest
    if tool.is_personal:
        return "Личный инструмент так не возвращают — только общий под наряд"
    if not tool.holder_id:
        return f"«{tool.name}» уже в шкафу"
    who = db.get(User, tool.holder_id)
    move(db, tool, None, f"Вернул кладовщик {by.full_name}" + (f" · у {who.full_name}" if who else "")
         + (f" · поломка: {note.strip()}" if broken else ""))
    if broken:
        tool.status = "broken"
    t_now = now()
    for r in db.scalars(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "return",
                                                  ToolRequest.status.in_(OPEN))):
        r.status, r.done_by, r.done_at = "done", by.id, t_now
        r.result, r.result_note = ("broken" if broken else "ok"), note.strip()[:300]
    for r in db.scalars(select(ToolRequest).where(ToolRequest.tool_id == tool.id, ToolRequest.kind == "issue",
                                                  ToolRequest.status == "done", ToolRequest.received_at.is_(None))):
        r.received_at, r.received_by = t_now, r.user_id
    return ""


def requests_list(db: Session, branch_id, status: str = "new", limit: int = 50) -> list[dict]:
    """Запросы для экрана «Выдача → Инструмент»: инструмент, кто, наряд, машина, место в шкафу."""
    from ..models import ToolRequest, Vehicle, WorkOrder
    q = select(ToolRequest).where(ToolRequest.branch_id == branch_id, ToolRequest.status == status)
    q = q.order_by(ToolRequest.created_at if status == "new" else ToolRequest.ready_at.desc() if status == "ready"
                   else ToolRequest.done_at.desc()).limit(limit)
    out = []
    for r in db.scalars(q):
        wo = db.get(WorkOrder, r.work_order_id) if r.work_order_id else None
        out.append({"r": r, "tool": db.get(Tool, r.tool_id), "who": db.get(User, r.user_id), "wo": wo,
                    "v": db.get(Vehicle, wo.vehicle_id) if wo else None,
                    "by": db.get(User, r.done_by) if r.done_by else None})
    return out


def out_on_orders(db: Session, branch_id) -> list[dict]:
    """Общий инструмент на руках под наряды: кто, с какого числа, какой наряд."""
    from ..models import WorkOrder
    out = []
    for t in db.scalars(select(Tool).where(Tool.branch_id == branch_id, Tool.holder_id.is_not(None), ~personal_filter())
                        .order_by(Tool.inv_no)):
        last = db.scalar(select(ToolMove).where(ToolMove.tool_id == t.id, ToolMove.kind == "issue")
                         .order_by(ToolMove.created_at.desc()))
        out.append({"t": t, "who": db.get(User, t.holder_id), "since": last.created_at if last else None,
                    "wo": db.get(WorkOrder, t.work_order_id) if t.work_order_id else None})
    out.sort(key=lambda x: x["since"] or now())
    return out


def holding(db: Session, user_id) -> list[Tool]:
    return db.scalars(select(Tool).where(Tool.holder_id == user_id, Tool.status.not_in(("lost", "written_off")))
                      .order_by(Tool.inv_no)).all()


def open_check(db: Session, user: User, kind: str) -> ToolCheck:
    """Новая проверка: в неё попадает весь инструмент, что сейчас числится за сотрудником.
    Результат пустой (11.11.2026): печатаем чистый бланк, проверяющий отмечает на бумаге, потом вносит сюда."""
    c = ToolCheck(user_id=user.id, kind=kind if kind in CHECK_KINDS else "planned")
    db.add(c)
    db.flush()
    for t in holding(db, user.id):
        db.add(ToolCheckLine(check_id=c.id, tool_id=t.id, result=""))
    return c


def finish_check(db: Session, c: ToolCheck) -> str:
    """Проверка проведена: состояние инструмента обновляется. При увольнении — всё возвращается на склад."""
    lines = db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id)).all()
    empty = sum(1 for l in lines if l.result not in CHECK_RESULTS)
    if empty:
        return f"Отметьте все позиции по бланку — не отмечено: {empty}"
    for l in lines:
        if l.result in ("fault", "lost") and l.charge <= 0 and not l.comment.strip():
            return "Для «сломан по вине» и «утерян» укажите сумму удержания или комментарий, почему без удержания"
    for l in lines:
        t = db.get(Tool, l.tool_id)
        t.status = {"ok": "ok", "worn": "worn", "fault": "broken", "lost": "lost"}[l.result]
        if l.result == "lost":
            db.add(ToolMove(tool_id=t.id, kind="lost", from_user=t.holder_id, comment="Утерян (проверка)"))
            t.holder_id = None
        elif c.kind == "dismissal":
            move(db, t, None, "Возврат при увольнении")
    c.status, c.done_at = "done", now()
    if not any(l.result in ("fault", "lost") and l.charge > 0 for l in lines):
        c.status = "approved"  # удерживать нечего — решение админа не нужно
    return ""


def approve_check(db: Session, c: ToolCheck, admin: User, approve_ids: set) -> Decimal:
    """Главный админ утверждает удержания: отмеченные строки → минус в ближайшей зарплате (одной суммой)."""
    total = Decimal(0)
    for l in db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id)):
        if l.result in ("fault", "lost") and l.charge > 0 and str(l.id) in approve_ids:
            t = db.get(Tool, l.tool_id)
            l.charge_approved = True
            what = "сломан по вине" if l.result == "fault" else "утерян"
            db.add(PayrollAccrual(user_id=c.user_id, base_amount=l.charge, rate=0, amount=-l.charge, on_date=date.today(),
                                  kind="deduction", note=f"Инструмент: {t.name} ({t.inv_no}) — {what}", tool_line_id=l.id))
            total += l.charge
    c.status, c.approved_by, c.approved_at = "approved", admin.id, now()
    return total
