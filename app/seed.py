"""Первичное наполнение базы: филиал, роли, права, подъёмники, точки доступа
и ТЕСТОВЫЕ сотрудники (для разработки). Список тестовых входов — docs/test-users.md.

Запускается автоматически при старте, если база пустая.
Справочник прав синхронизируется при каждом старте.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DEMO, DEMO_PASSWORD
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
    ("director", "Виктор Директоров", "director",    "director123", "1007", "0000100008"),
    ("deputy",  "Наталья Заместитель", "deputy",     "deputy123",  "1008", "0000100009"),
    ("buh",     "Оксана Бухгалтерова", "accountant", "buh123",     "1009", "0000100010"),
    # 12.11.2026: новые мастера
    ("elektro", "Роман Электриков",   "electrician", "elektro123", "1010", "0000100011"),
    ("motor",   "Василий Моторов",    "motorist",    "motor123",   "1011", "0000100012"),
    ("moyka",   "Артём Мойкин",       "washer",      "moyka123",   "1012", "0000100013"),
    ("detail",  "Ольга Полирова",     "detailer",    "detail123",  "1013", "0000100014"),
]


def sync_permissions(db: Session):
    existing = {p.code: p for p in db.scalars(select(Permission))}
    first_run = not existing
    new_codes = []
    for code, name, group in PERMISSIONS:
        if code in existing:
            existing[code].name, existing[code].group = name, group
        else:
            db.add(Permission(code=code, name=name, group=group))
            new_codes.append(code)
    db.flush()
    if first_run or not new_codes:
        return
    # Появилось новое право (новая версия программы) — выдаём его ролям, у которых оно
    # есть по умолчанию. Остальные права, которые Стас уже настроил руками, не трогаем.
    for role in db.scalars(select(Role)):
        defaults = DEFAULT_ROLES.get(role.code, (None, None, []))[2]
        for code in new_codes:
            if code in defaults:
                current_branch_id.set(role.branch_id)
                db.add(RolePermission(role_id=role.id, permission=code, branch_id=role.branch_id))
    db.flush()


def _add_user(db: Session, login, name, role: Role, pwd, pin, card):
    if DEMO:
        db.add(User(full_name=name, login=login, role_id=role.id, password_hash=hash_secret(DEMO_PASSWORD), pin_hash=""))
        return
    u = User(full_name=name, login=login, role_id=role.id, password_hash=hash_secret(pwd), pin_hash=hash_secret(pin))
    db.add(u)
    db.flush()
    if not db.scalar(select(StaffCard).where(StaffCard.uid == card)):
        db.add(StaffCard(user_id=u.id, uid=card, label="тестовая"))


def sync_roles(db: Session):
    """Новая версия принесла новую роль (директор, зам) — добавляем её в уже работающую базу
    вместе с тестовым сотрудником. Роли, которые Стас уже настроил, не трогаем."""
    for branch in db.scalars(select(Branch)):
        have = {r.code for r in db.scalars(select(Role).where(Role.branch_id == branch.id))}
        current_branch_id.set(branch.id)
        for i, (code, (name, color, perms)) in enumerate(DEFAULT_ROLES.items()):
            if code in have:
                # Роль есть, а сотрудников в ней нет совсем (роль появилась раньше тестового входа) — заводим тестового
                r = db.scalar(select(Role).where(Role.branch_id == branch.id, Role.code == code))
                if r and not db.scalar(select(User.id).where(User.role_id == r.id).limit(1)):
                    for login, uname, role, pwd, pin, card in TEST_USERS:
                        if role == code and not db.scalar(select(User).where(User.login == login)):
                            _add_user(db, login, uname, r, pwd, pin, card)
                continue
            r = Role(code=code, name=name, color=color, is_system=True, sort=i, branch_id=branch.id)
            db.add(r)
            db.flush()
            for p in perms:
                db.add(RolePermission(role_id=r.id, permission=p, branch_id=branch.id))
            for login, uname, role, pwd, pin, card in TEST_USERS:
                if role == code and not db.scalar(select(User).where(User.login == login)):
                    _add_user(db, login, uname, r, pwd, pin, card)
    db.flush()


def seed():
    with SessionLocal() as db:
        sync_permissions(db)
        if db.scalar(select(Branch)) is not None:
            sync_roles(db)
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

        lifts = []
        for n in range(1, 11):
            lift = Lift(number=n, name=f"Пост {n}")
            db.add(lift)
            lifts.append(lift)
        db.add(AccessPoint(name="Вход в цех", kind="gate"))
        db.add(AccessPoint(name="Склад", kind="gate"))
        db.add(AccessPoint(name="ПК приёмки", kind="login", counts_presence=False))

        for login, name, role, pwd, pin, card in TEST_USERS:
            if DEMO:
                # Публичное демо: пароль из настроек хостинга, PIN и карт нет
                u = User(full_name=name, login=login, role_id=roles[role].id,
                         password_hash=hash_secret(DEMO_PASSWORD), pin_hash="")
                db.add(u)
                continue
            u = User(full_name=name, login=login, role_id=roles[role].id,
                     password_hash=hash_secret(pwd), pin_hash=hash_secret(pin))
            db.add(u)
            db.flush()
            db.add(StaffCard(user_id=u.id, uid=card, label="тестовая"))
        # Тестовым слесарям — по посту (остальные посты Стас закрепит в настройках)
        db.flush()
        mechs = db.scalars(select(User).join(Role, User.role_id == Role.id).where(Role.code == "mechanic")
                           .order_by(User.login)).all()
        for lift, m in zip(lifts, mechs):
            lift.mechanic_id = m.id
        db.commit()
