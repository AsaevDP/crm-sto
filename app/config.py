"""Настройки запуска. Берутся из переменных окружения или файла .env в корне."""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MEDIA_DIR = ROOT / "media"
DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)


def _load_dotenv():
    """Простейшее чтение .env (без лишних зависимостей)."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# На шаге 8 здесь будет postgresql+psycopg://...
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'crm.db').as_posix()}")


def _secret_key() -> str:
    """Ключ подписи сессий. Генерируется один раз и хранится в data/secret.key."""
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


SECRET_KEY = _secret_key()
PORT = int(os.getenv("PORT", "8810"))

# Демо-режим (публичный показ на хостинге, напр. Render):
# - у тестовых сотрудников пароль из DEMO_PASSWORD (не из открытого репозитория);
# - вход по PIN и картам выключен (их значения есть в открытом коде);
# - сверху плашка «Демо-версия».
DEMO = os.getenv("DEMO") == "1"
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "")
if DEMO and len(DEMO_PASSWORD) < 8:
    raise RuntimeError("В демо-режиме нужна переменная DEMO_PASSWORD (от 8 символов)")
# Cookie только по HTTPS (на хостинге и на шаге 8)
COOKIE_SECURE = os.getenv("COOKIE_SECURE") == "1"
