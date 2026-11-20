"""Ремонт закончен — машина на мойку (20.11.2026, брат, наряд 20). Слесарь закрыл последнюю свою работу, осталась
мойка: менеджеру «Мастера закончили — машина ждёт мойки», мойщику «Машина с ремонта — можно мыть»; машина в очереди
встаёт перед серыми; пост освобождается; у слесаря наряд — в плитке «Сделал», не в «В работе»; в наряде — плашка."""
from sqlalchemy import select

from app.db import SessionLocal, current_branch_id
from app.models import Lift, Notification, WorkOrder, WoWork
from app.services import wash as wsvc

from .conftest import login
from .test_wash_queue import _repair_car, _user


def test_repair_done_goes_to_wash(client):
    grey = _repair_car(client, "АА 2020 ГР", "0672020201", status="approve")     # серая — не согласовано
    wo = _repair_car(client, "АА 2020 РМ", "0672020202", status="work")
    current_branch_id.set(wo.branch_id)
    andrey, moyka, irina = _user("andrey"), _user("moyka"), _user("irina")
    with SessionLocal() as db:
        o = db.get(WorkOrder, wo.id)
        lift = db.scalar(select(Lift).where(Lift.branch_id == o.branch_id, Lift.active.is_(True)))
        o.lift_id, o.manager_id = lift.id, irina.id
        db.get(WorkOrder, grey.id).queue_rank = 1    # серая стоит выше по порядку менеджера
        o.queue_rank = 2
        db.commit()
        w = db.scalar(select(WoWork).where(WoWork.work_order_id == wo.id, WoWork.mechanic_id == andrey.id))
    login(client, "andrey", "andrey123")
    for a in ("start", "done"):
        client.post(f"/orders/{wo.id}/works/{w.id}/state", data={"action": a})
    with SessionLocal() as db:
        o = db.get(WorkOrder, wo.id)
        assert o.status == "work" and o.lift_id is None                       # пост свободен
        kinds = {(n.user_id, n.kind) for n in db.scalars(select(Notification).where(Notification.work_order_id == wo.id))}
        assert (irina.id, "to_wash") in kinds and (moyka.id, "wash_go") in kinds
        q = [c.o.id for c in wsvc.board(db, o.branch_id)["queue"]]
        assert q.index(wo.id) < q.index(grey.id)                              # перед серой
    page = client.get("/orders?tab=mydone").text
    assert "Сделал" in page and "АА 2020 РМ".replace(" ", "") in page.replace(" ", "")
    assert "АА2020РМ" not in client.get("/orders?tab=work").text.replace(" ", "")
    assert "Ремонт закончен — машина на мойке" in client.get(f"/orders/{wo.id}").text
