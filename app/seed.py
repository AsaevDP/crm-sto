"""Первичное наполнение базы: филиал, роли, права, подъёмники, точки доступа
и ТЕСТОВЫЕ сотрудники (для разработки). Список тестовых входов — docs/test-users.md.

Запускается автоматически при старте, если база пустая.
Справочник прав синхронизируется при каждом старте.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, current_branch_id
from .models import AccessPoint, Branch, Lift, Permission, Role, RolePermission, StaffCard, User
from .permissions import DEFAULT_ROLES, PERMISSIONS
from .security import hash_secret

# ТЕСТОВЫЕ сотрудники: (логин, ФИО, роль, пароль, PIN, номер карты)
TEST_USERS = [
    ("admin",   "Стас Админов",       "admin",       "admin123",   "1000", "0000100001"),
    ("irina",   "Ирина Менеджерова",  "manager",     "irina123",   "1001", "0000100002"),
    ("oleg",    "Олег Запчастин",     "partsman",    "oleg123",    "1002", "0000100003"),
    ("pavel",   "Павел Складской",    "storekeeper", "pavel123",   "1003", "0000100004"),
    ("dmitro",  "Дмитро Разборов",    "content",     "dmitro123",  "1004", "0000100005"),
    ("andrey",  "Андрей Демченко",    "mechanic",    "andrey123",  "1005", "0000100006"),
    ("sergey",  "Сергей Ключников",   "mechanic",    "sergey123",  "1006", "0000100007"),
]


def sync_permissions(db: Session):
    existing = {p.code: p for p in db.scalars(select(Permission))}
    for code, name, group in PERMISSIONS:
        if code in existing:
            existing[code].name, existing[code].group = name, group
        else:
            db.add(Permission(code=code, name=name, group=group))
    db.flush()


def seed():
    with SessionLocal() as db:
        sync_permissions(db)
        if db.scalar(select(Branch)) is not None:
            db.commit()
            return
        branch = Branch(name="СТО №1", city="", code="СТО1")
        db.add(branch)
        db.flush()
        current_branch_id.set(branch.id)

        roles = {}
        for i, (code, (name, color, perms)) in enumerate(DEFAULT_ROLES.items()):
            r = Role(code=code, name=name, color=color, is_system=True, sort=i, branch_id=branch.id)
            db.add(r)
            db.flush()
            roles[code] = r
            for p in perms:
                db.add(RolePermission(role_id=r.id, permission=p))

        for n in range(1, 11):
            db.add(Lift(number=n, name=f"Подъёмник {n}"))
        db.add(AccessPoint(name="Вход в цех", kind="gate"))
        db.add(AccessPoint(name="Склад", kind="gate"))
        db.add(AccessPoint(name="ПК приёмки", kind="login", counts_presence=False))

        for login, name, role, pwd, pin, card in TEST_USERS:
            u = User(full_name=name, login=login, role_id=roles[role].id,
                     password_hash=hash_secret(pwd), pin_hash=hash_secret(pin))
            db.add(u)
            db.flush()
            db.add(StaffCard(user_id=u.id, uid=card, label="тестовая"))
        db.commit()
