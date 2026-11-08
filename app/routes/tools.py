"""Шаг 6а: учёт инструмента — справочник, выдача/возврат, проверки, удержания, акт."""
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select

from ..models import Tool, ToolCheck, ToolCheckLine, ToolMove, User
from ..models.tools import CHECK_KINDS, CHECK_RESULTS, CHECK_STATUS, TOOL_STATUS
from ..services import orders as osvc
from ..services import tools as svc
from ..web import Ctx, Forbidden, flash, get_ctx
from .orders import _uuid

router = APIRouter()


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or "0").replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return Decimal(0)


def _to(url):
    return RedirectResponse(url, status_code=303)


def _staff(ctx: Ctx):
    return ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id, User.active.is_(True)).order_by(User.full_name)).all()


def _tool(ctx: Ctx, tid: str) -> Tool:
    t = ctx.db.get(Tool, _uuid(tid))
    if not t or t.branch_id != ctx.branch.id:
        raise Forbidden()
    if not ctx.can("tools.view") and t.holder_id != ctx.user.id:
        raise Forbidden()
    return t


async def _photo_from(form) -> str | None:
    f = form.get("photo")
    if f is not None and hasattr(f, "read"):
        data = await f.read()
        return svc.save_photo(data) if data else None
    return None


# ---------- Список ----------
@router.get("/tools")
def tools_list(ctx: Ctx = Depends(get_ctx), q: str = "", holder: str = ""):
    ctx.need("tools.view", "tools.view_own")
    if not ctx.can("tools.view"):
        return _to(f"/tools/staff/{ctx.user.id}")
    db = ctx.db
    query = select(Tool).where(Tool.branch_id == ctx.branch.id, Tool.status != "written_off")
    if holder == "stock":
        query = query.where(Tool.holder_id.is_(None))
    elif holder:
        query = query.where(Tool.holder_id == _uuid(holder))
    rows = db.scalars(query.order_by(Tool.inv_no)).all()
    if q.strip():
        ql = q.strip().lower()
        rows = [t for t in rows if ql in f"{t.inv_no} {t.serial} {t.name} {t.description}".lower()]
    staff = _staff(ctx)
    people = {u.id: u for u in staff}
    summary = {}
    for t in db.scalars(select(Tool).where(Tool.branch_id == ctx.branch.id, Tool.holder_id.is_not(None),
                                           Tool.status.not_in(("lost", "written_off")))):
        n, s = summary.get(t.holder_id, (0, Decimal(0)))
        summary[t.holder_id] = (n + 1, s + t.cost)
    pending = db.scalars(select(ToolCheck).where(ToolCheck.branch_id == ctx.branch.id, ToolCheck.status != "approved")
                         .order_by(ToolCheck.created_at.desc())).all()
    return ctx.render("tools/list.html", title="Инструмент", rows=rows, q=q, holder=holder, staff=staff, people=people,
                      summary=summary, pending=pending, TOOL_STATUS=TOOL_STATUS, CHECK_KINDS=CHECK_KINDS,
                      CHECK_STATUS=CHECK_STATUS, next_inv=svc.next_inv_no(db, ctx.branch.id))


@router.post("/tools")
async def tool_add(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.manage")
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "Название обязательно", "error")
        return _to("/tools")
    inv = str(form.get("inv_no", "")).strip()[:40] or svc.next_inv_no(ctx.db, ctx.branch.id)
    if ctx.db.scalar(select(Tool.id).where(Tool.branch_id == ctx.branch.id, Tool.inv_no == inv)):
        flash(request, f"Инвентарный номер {inv} уже занят", "error")
        return _to("/tools")
    t = Tool(inv_no=inv, name=name[:200], serial=str(form.get("serial", "")).strip()[:80],
             description=str(form.get("description", "")).strip(), cost=_dec(form.get("cost")),
             photo=(await _photo_from(form)) or "")
    ctx.db.add(t)
    ctx.db.flush()
    holder = form.get("holder_id")
    u = ctx.db.get(User, _uuid(str(holder))) if holder else None
    if u and u.branch_id == ctx.branch.id:
        svc.move(ctx.db, t, u, "Выдан при заведении")
    ctx.db.commit()
    flash(request, f"Инструмент {t.inv_no} заведён")
    return _to(f"/tools/{t.id}")


# ---------- Карточка инструмента ----------
@router.get("/tools/{tid}")
def tool_view(tid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.view", "tools.view_own")
    t = _tool(ctx, tid)
    db = ctx.db
    moves = db.scalars(select(ToolMove).where(ToolMove.tool_id == t.id).order_by(ToolMove.created_at.desc())).all()
    checks = db.execute(select(ToolCheckLine, ToolCheck).join(ToolCheck, ToolCheck.id == ToolCheckLine.check_id)
                        .where(ToolCheckLine.tool_id == t.id).order_by(ToolCheck.created_at.desc())).all()
    people = {u.id: u for u in db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return ctx.render("tools/view.html", title=t.name, t=t, moves=moves, checks=checks, people=people, staff=_staff(ctx),
                      TOOL_STATUS=TOOL_STATUS, CHECK_KINDS=CHECK_KINDS, CHECK_RESULTS=CHECK_RESULTS)


@router.post("/tools/{tid}")
async def tool_edit(tid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.manage")
    t = _tool(ctx, tid)
    form = await request.form()
    action = str(form.get("action", "save"))
    if action == "move":
        u = ctx.db.get(User, _uuid(str(form.get("to_user")))) if form.get("to_user") else None
        svc.move(ctx.db, t, u if u and u.branch_id == ctx.branch.id else None, str(form.get("comment", "")))
        flash(request, f"Выдан: {u.full_name}" if u else "Возвращён на склад инструмента")
    elif action == "writeoff":
        ctx.db.add(ToolMove(tool_id=t.id, kind="writeoff", from_user=t.holder_id, comment=str(form.get("comment", ""))[:500]))
        t.status, t.holder_id = "written_off", None
        flash(request, "Списан")
    else:
        t.name = str(form.get("name", t.name)).strip()[:200] or t.name
        t.serial = str(form.get("serial", "")).strip()[:80]
        t.description = str(form.get("description", "")).strip()
        t.cost = _dec(form.get("cost"))
        if form.get("status") in TOOL_STATUS and form.get("status") != "written_off":
            t.status = str(form.get("status"))
        photo = await _photo_from(form)
        if photo:
            t.photo = photo
        flash(request, "Сохранено")
    ctx.db.commit()
    return _to(f"/tools/{tid}")


@router.get("/tools/files/{name}")
def tool_file(name: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.view", "tools.view_own")
    path = (svc.TOOL_PHOTO_DIR / name).resolve()
    if svc.TOOL_PHOTO_DIR.resolve() not in path.parents or not path.exists():
        raise Forbidden()
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


# ---------- Сотрудник: что за ним числится, проверки ----------
@router.get("/tools/staff/{uid}")
def staff_tools(uid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.view", "tools.view_own")
    u = ctx.db.get(User, _uuid(uid))
    if not u or u.branch_id != ctx.branch.id or (not ctx.can("tools.view") and u.id != ctx.user.id):
        raise Forbidden()
    tools = svc.holding(ctx.db, u.id)
    checks = ctx.db.scalars(select(ToolCheck).where(ToolCheck.user_id == u.id).order_by(ToolCheck.created_at.desc())).all()
    return ctx.render("tools/staff.html", title=f"Инструмент: {u.full_name}", u=u, tools=tools, checks=checks,
                      total=sum((t.cost for t in tools), Decimal(0)), TOOL_STATUS=TOOL_STATUS, CHECK_KINDS=CHECK_KINDS,
                      CHECK_STATUS=CHECK_STATUS, is_me=u.id == ctx.user.id)


@router.post("/tools/staff/{uid}/checks")
async def check_new(uid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.manage")
    u = ctx.db.get(User, _uuid(uid))
    if not u or u.branch_id != ctx.branch.id:
        raise Forbidden()
    form = await request.form()
    c = svc.open_check(ctx.db, u, str(form.get("kind", "planned")))
    ctx.db.commit()
    return _to(f"/tools/checks/{c.id}")


# ---------- Проверка ----------
def _check(ctx: Ctx, cid: str) -> ToolCheck:
    c = ctx.db.get(ToolCheck, _uuid(cid))
    if not c or c.branch_id != ctx.branch.id or (not ctx.can("tools.view") and c.user_id != ctx.user.id):
        raise Forbidden()
    return c


def _check_data(ctx: Ctx, c: ToolCheck) -> dict:
    lines = ctx.db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id).order_by(ToolCheckLine.created_at)).all()
    tools = {t.id: t for t in ctx.db.scalars(select(Tool).where(Tool.id.in_([l.tool_id for l in lines])))}
    people = {u.id: u for u in ctx.db.scalars(select(User).where(User.branch_id == ctx.branch.id))}
    return dict(c=c, lines=lines, tools=tools, people=people, u=people.get(c.user_id), CHECK_KINDS=CHECK_KINDS,
                CHECK_RESULTS=CHECK_RESULTS, CHECK_STATUS=CHECK_STATUS,
                charge_total=sum((l.charge for l in lines if l.result in ("fault", "lost")), Decimal(0)))


@router.get("/tools/checks/{cid}")
def check_view(cid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.view", "tools.view_own")
    c = _check(ctx, cid)
    return ctx.render("tools/check.html", title="Проверка инструмента", **_check_data(ctx, c),
                      can_edit=ctx.can("tools.manage") and c.status == "draft",
                      can_approve=ctx.user.role.code == "admin" and c.status == "done")


@router.post("/tools/checks/{cid}")
async def check_save(cid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Кладовщик отмечает каждую позицию; «Завершить» — состояние инструмента обновляется."""
    ctx.need("tools.manage")
    c = _check(ctx, cid)
    if c.status != "draft":
        raise Forbidden()
    form = await request.form(max_files=200)
    for l in ctx.db.scalars(select(ToolCheckLine).where(ToolCheckLine.check_id == c.id)):
        k = str(l.id)
        res = str(form.get(f"r_{k}", l.result))
        if res in CHECK_RESULTS:
            l.result = res
        l.comment = str(form.get(f"c_{k}", l.comment)).strip()[:500]
        tool = ctx.db.get(Tool, l.tool_id)
        raw = form.get(f"m_{k}")
        # По вине / утерян: по умолчанию удержание = стоимость инструмента (кладовщик может поправить)
        l.charge = _dec(raw) if raw not in (None, "") else (tool.cost if l.result in ("fault", "lost") else Decimal(0))
        if l.result in ("ok", "worn"):
            l.charge = Decimal(0)
        f = form.get(f"p_{k}")
        if f is not None and hasattr(f, "read"):
            data = await f.read()
            if data:
                l.photo = svc.save_photo(data) or l.photo
    c.note = str(form.get("note", c.note)).strip()
    if form.get("action") == "finish":
        err = svc.finish_check(ctx.db, c)
        if err:
            ctx.db.rollback()
            flash(request, err, "error")
            return _to(f"/tools/checks/{cid}")
        flash(request, "Проверка завершена" + (" — удержания ждут решения главного админа" if c.status == "done" else ""))
    else:
        flash(request, "Сохранено")
    ctx.db.commit()
    return _to(f"/tools/checks/{cid}")


@router.post("/tools/checks/{cid}/approve")
async def check_approve(cid: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Только главный админ: какие удержания провести в зарплату."""
    if ctx.user.role.code != "admin":
        raise Forbidden()
    c = _check(ctx, cid)
    if c.status != "done":
        raise Forbidden()
    form = await request.form()
    total = svc.approve_check(ctx.db, c, ctx.user, {str(x) for x in form.getlist("approve")})
    ctx.db.commit()
    flash(request, f"Удержано из зарплаты: {total} ₴" if total else "Решение принято, без удержаний")
    return _to(f"/tools/checks/{cid}")


@router.get("/tools/checks/{cid}/print")
def check_print(cid: str, ctx: Ctx = Depends(get_ctx)):
    ctx.need("tools.view", "tools.view_own")
    c = _check(ctx, cid)
    return ctx.render("tools/check_print.html", title="Акт проверки инструмента", **_check_data(ctx, c),
                      fmt_phone=osvc.fmt_phone)
