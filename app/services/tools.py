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
