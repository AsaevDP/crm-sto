"""«✓ Получил» — мастер подтверждает, что выданное у него в руках (20.11.2026, брат).

Запчасти: кладовщик или менеджер нажал «Выдано» → у мастера кнопка «Получил». Пока не подтвердил — «Начал» по его
работам серая (человеческий фактор: принесли, оставили, ушли — мастер не заметил). Если в наряде несколько мастеров,
подтверждает тот, для чьей работы запчасти; деталь без работы — ведущий мастер. Ведущий может подтвердить всё.
Инструмент: тот же «Получил» после «Выдано», работу не блокирует.
Всё, что выдали до 20.11.2026, отмечается «получено» один раз при запуске (`fill_old`)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now
from ..models import IssueRequest, IssueRequestLine, User, WoPart, WorkOrder, WoWork


def unreceived(db: Session, wo_id) -> list[IssueRequest]:
    """Выданные запросы наряда, которые мастер ещё не подтвердил."""
    return db.scalars(select(IssueRequest).where(IssueRequest.work_order_id == wo_id, IssueRequest.status == "issued",
                                                 IssueRequest.received_at.is_(None))
                      .order_by(IssueRequest.issued_at)).all()


def owners(db: Session, wo: WorkOrder, req: IssueRequest) -> set:
    """Кому подтверждать: мастера работ, к которым привязаны детали запроса; без работы — ведущий мастер."""
    out = set()
    for line in db.scalars(select(IssueRequestLine).where(IssueRequestLine.request_id == req.id)):
        p = db.get(WoPart, line.wo_part_id)
        w = db.get(WoWork, p.wo_work_id) if p and p.wo_work_id else None
        who = (w.mechanic_id if w and w.mechanic_id else None) or wo.mechanic_id
        if who:
            out.add(who)
    if not out and wo.mechanic_id:
        out.add(wo.mechanic_id)
    return out


def can_receive(db: Session, user: User, wo: WorkOrder, req: IssueRequest) -> bool:
    if req.status != "issued" or req.received_at:
        return False
    return user.role.code == "admin" or user.id == wo.mechanic_id or user.id in owners(db, wo, req)


def blocked_works(db: Session, wo: WorkOrder) -> set:
    """Мастера, у которых в этом наряде есть неподтверждённая выдача (их «Начал» серая)."""
    out = set()
    for r in unreceived(db, wo.id):
        out |= owners(db, wo, r)
    return out


def start_blocked(db: Session, wo: WorkOrder, w: WoWork) -> bool:
    """«Начал» по работе нельзя: её мастеру выдали запчасти, а он не нажал «Получил»."""
    from .orders import work_owner
    if w.status != "todo" and w.status != "paused":
        return False
    who = work_owner(wo, w)
    return bool(who and who in blocked_works(db, wo))


def cards(db: Session, wo: WorkOrder, user: User) -> list[dict]:
    """Для наряда: неподтверждённые выдачи — что выдали, кто выдал, кому подтверждать, может ли этот пользователь."""
    out = []
    for r in unreceived(db, wo.id):
        lines = []
        for line in db.scalars(select(IssueRequestLine).where(IssueRequestLine.request_id == r.id)):
            p = db.get(WoPart, line.wo_part_id)
            if p:
                lines.append({"name": p.name, "qty": line.issued_qty or line.qty})
        who = [db.get(User, u) for u in owners(db, wo, r)]
        out.append({"r": r, "lines": lines, "who": [u for u in who if u], "by": db.get(User, r.issued_by) if r.issued_by else None,
                    "can": can_receive(db, user, wo, r)})
    return out


def receive(db: Session, req: IssueRequest, user: User):
    req.received_at, req.received_by = now(), user.id
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.work_order_id == req.work_order_id,
                                                   Notification.user_id == user.id, Notification.kind == "issued",
                                                   Notification.read_at.is_(None))):
        n.read_at = now()


def for_master(db: Session, branch_id, user_id) -> list[tuple]:
    """Неподтверждённые выдачи запчастей, которые ждут этого мастера: [(запрос, наряд)]."""
    out = []
    for r, wo in db.execute(select(IssueRequest, WorkOrder).join(WorkOrder, WorkOrder.id == IssueRequest.work_order_id)
                            .where(IssueRequest.branch_id == branch_id, IssueRequest.status == "issued",
                                   IssueRequest.received_at.is_(None))):
        if user_id in owners(db, wo, r):
            out.append((r, wo))
    return out


# ---------- инструмент ----------
def tools_for_master(db: Session, branch_id, user_id) -> list:
    from ..models import ToolRequest
    return db.scalars(select(ToolRequest).where(ToolRequest.branch_id == branch_id, ToolRequest.user_id == user_id,
                                                ToolRequest.kind == "issue", ToolRequest.status == "done",
                                                ToolRequest.received_at.is_(None))).all()


def receive_tool(db: Session, req, user: User) -> str:
    from ..models import Tool
    tool = db.get(Tool, req.tool_id)
    if req.kind != "issue" or req.status != "done" or req.received_at:
        return "Уже подтверждено"
    if req.user_id != user.id and user.role.code != "admin":
        return "Подтверждает тот, кто просил инструмент"
    if not tool or tool.holder_id != req.user_id:
        return "Инструмент уже не у вас — его вернули в шкаф"
    req.received_at, req.received_by = now(), user.id
    from ..models import Notification
    for n in db.scalars(select(Notification).where(Notification.user_id == req.user_id, Notification.kind == "tool_issued",
                                                   Notification.read_at.is_(None), Notification.text.startswith(tool.name))):
        n.read_at = now()   # подтвердил — окошко «выдано, подтвердите» больше не нужно
    return ""


def fill_old(db: Session):
    """Один раз (20.11.2026): всё, что выдали раньше, — «получено», чтобы машины в работе не встали."""
    from ..models import Branch, ToolRequest
    from ..db import current_branch_id
    from . import settings as st
    for br in db.scalars(select(Branch)):
        if st.get(db, br.id, "grants_once").get("receipt.fill_old"):
            continue
        current_branch_id.set(br.id)
        for r in db.scalars(select(IssueRequest).where(IssueRequest.branch_id == br.id, IssueRequest.status == "issued",
                                                       IssueRequest.received_at.is_(None))):
            r.received_at, r.received_by = r.issued_at or r.updated_at, r.issued_by
        for r in db.scalars(select(ToolRequest).where(ToolRequest.branch_id == br.id, ToolRequest.kind == "issue",
                                                      ToolRequest.status == "done", ToolRequest.received_at.is_(None))):
            r.received_at, r.received_by = r.done_at or r.updated_at, r.user_id
        st.put(db, br.id, "grants_once", {"receipt.fill_old": True})
    db.flush()
