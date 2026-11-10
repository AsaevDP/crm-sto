"""Инструмент: выдача/возврат, проверки (приём, увольнение, плановая), удержания из зарплаты."""
import secrets
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now
from ..models import PayrollAccrual, Tool, ToolCheck, ToolCheckLine, ToolMove, User
from ..models.tools import CHECK_KINDS
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
    """Условие «личный инструмент» (у старого инструмента категории нет — он личный)."""
    from sqlalchemy import or_
    return or_(Tool.category == "personal", Tool.category.is_(None), Tool.category == "")


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
def open_request(db: Session, tool_id):
    from ..models import ToolRequest
    return db.scalar(select(ToolRequest).where(ToolRequest.tool_id == tool_id, ToolRequest.status == "new"))


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


def complete_request(db: Session, req, by: User, result: str = "ok", note: str = "") -> str:
    """Кладовщик: выдал (issue) или принял (return, ok / broken с комментарием)."""
    from ..models import WorkOrder, WoWork
    tool = db.get(Tool, req.tool_id)
    who = db.get(User, req.user_id)
    if req.status != "new" or not tool or not who:
        return "Запрос уже закрыт"
    if req.kind == "issue":
        if tool.holder_id:
            return f"«{tool.name}» уже на руках — сначала его должны сдать"
        wo = db.get(WorkOrder, req.work_order_id) if req.work_order_id else None
        work = db.get(WoWork, req.wo_work_id) if req.wo_work_id else None
        move(db, tool, who, f"По запросу слесаря{' · наряд ' + wo.number if wo else ''}", wo=wo, work=work)
    else:
        move(db, tool, None, "Сдал в шкаф" + (f" · поломка: {note.strip()}" if result == "broken" else ""))
        if result == "broken":
            tool.status = "broken"
        req.result, req.result_note = ("broken" if result == "broken" else "ok"), note.strip()[:300]
    req.status, req.done_by, req.done_at = "done", by.id, now()
    return ""


def requests_list(db: Session, branch_id, status: str = "new", limit: int = 50) -> list[dict]:
    """Запросы для экрана «Выдача → Инструмент»: инструмент, кто, наряд, машина, место в шкафу."""
    from ..models import ToolRequest, Vehicle, WorkOrder
    q = select(ToolRequest).where(ToolRequest.branch_id == branch_id, ToolRequest.status == status)
    q = q.order_by(ToolRequest.created_at if status == "new" else ToolRequest.done_at.desc()).limit(limit)
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
    """Новая проверка: в неё попадает весь инструмент, что сейчас числится за сотрудником."""
    c = ToolCheck(user_id=user.id, kind=kind if kind in CHECK_KINDS else "planned")
    db.add(c)
    db.flush()
    for t in holding(db, user.id):
        db.add(ToolCheckLine(check_id=c.id, tool_id=t.id, result="ok"))
    return c


def finish_check(db: Session, c: ToolCheck) -> str:
    """Проверка проведена: состояние инструмента обновляется. При увольнении — всё возвращается на склад."""
    lines = db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id)).all()
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
                                  kind="deduction", note=f"Удержание: {t.name} ({t.inv_no}) — {what}"))
            total += l.charge
    c.status, c.approved_by, c.approved_at = "approved", admin.id, now()
    return total
