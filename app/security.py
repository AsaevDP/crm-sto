"""Хэширование паролей и PIN (встроенный hashlib, без внешних библиотек)
и простая защита от подбора PIN."""
import hashlib
import hmac
import secrets
import time

_ITER = 200_000


def hash_secret(raw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), _ITER).hex()
    return f"pbkdf2${_ITER}${salt}${h}"


def check_secret(raw: str, stored: str) -> bool:
    if not stored or not raw:
        return False
    try:
        _, it, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), int(it)).hex()
    return hmac.compare_digest(calc, h)


# Блокировка после 5 неудачных попыток на 60 секунд (по ключу: логин или id сотрудника).
# Хранится в памяти: после перезапуска сервера сбрасывается — для локальной сети достаточно.
_FAILS: dict[str, list[float]] = {}
MAX_FAILS, LOCK_SEC = 5, 60


def is_locked(key: str) -> int:
    """Сколько секунд ещё заблокировано (0 — не заблокировано)."""
    now = time.time()
    fails = [t for t in _FAILS.get(key, []) if now - t < LOCK_SEC]
    _FAILS[key] = fails
    if len(fails) >= MAX_FAILS:
        return int(LOCK_SEC - (now - fails[0])) + 1
    return 0


def register_fail(key: str):
    _FAILS.setdefault(key, []).append(time.time())


def reset_fails(key: str):
    _FAILS.pop(key, None)
