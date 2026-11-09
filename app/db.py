"""Подключение к БД и базовый класс таблиц."""
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, TypeDecorator, Uuid, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, sessionmaker

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

if DATABASE_URL.startswith("sqlite"):
    # В SQLite проверка внешних ключей по умолчанию выключена — включаем.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # чтобы чтение не блокировалось записью
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Кто сейчас работает и в каком филиале — заполняется при каждом запросе.
# Нужно, чтобы автоматически проставлять created_by / branch_id и писать журнал.
current_user_id: ContextVar[uuid.UUID | None] = ContextVar("current_user_id", default=None)
current_branch_id: ContextVar[uuid.UUID | None] = ContextVar("current_branch_id", default=None)


def now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Дата-время всегда в UTC и всегда «с поясом».
    SQLite не хранит часовой пояс и отдаёт время без него — тогда .astimezone() в шаблонах считал UTC местным
    временем, и все часы на экранах были на 2 часа раньше (исправлено 10.11.2026). Тут: пишем в UTC,
    читаем — помечаем как UTC. В PostgreSQL (шаг 8) пояс хранится и так."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, datetime):
            value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)  # в SQLite храним как раньше: UTC без пояса
        return value

    def process_result_value(self, value, dialect):
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class Entity(Base):
    """Общие поля всех таблиц: id UUID, branch_id, created_at, updated_at, created_by."""
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now, onupdate=now)

    @declared_attr
    def branch_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid, ForeignKey("branches.id"), index=True)

    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid, ForeignKey("users.id", use_alter=True), nullable=True)


def add_missing_columns():
    """До Alembic (шаг 8): если в модели появилась новая колонка, а таблица уже есть —
    добавляем колонку, не трогая данные. Удаление/переименование так не делается."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
