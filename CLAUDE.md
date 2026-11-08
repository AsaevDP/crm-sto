# CRM СТО — правила проекта

Полное ТЗ: [BRIEF.md](BRIEF.md) (раздел 8а — решения и дополнения). Читай его перед работой.

## Заказчик
- Стас, дизайнер, не программист. Общение на русском: коротко, списками.
- Задача сложнее одного шага → как понял (2–3 строки) → вопросы → план «шаг → как проверим» → ждём «го».

## Жёсткие правила
- **Ни одного запроса к внешним доменам.** Все JS/CSS/шрифты/иконки лежат в `app/static/`. Никаких CDN, Google Fonts.
- Шрифт — системный (`-apple-system`, `Segoe UI`), чтобы не тащить файлы.
- Все таблицы: `id UUID`, `branch_id`, `created_at`, `updated_at`, `created_by`.
- Остатки склада не хранятся числом — считаются по `stock_movements`.
- Деньги — `NUMERIC(12,2)`, никаких float.
- Каждое изменение данных → запись в `audit_log`.
- Интерфейс только на русском.
- Цвета только через CSS-переменные из `app/static/css/app.css` (акцент меняется в настройках и по роли).

## Запуск (dev, до шага 8)
- Python 3.11 напрямую, без Docker. Порт **8810** (реестр: `F:\Claude\PORTS.md`).
- `run.bat` — сам создаёт `.venv`, ставит библиотеки, слушает `0.0.0.0:8810` (доступ с iPhone по Wi-Fi).
- Preview в Claude: `crmsto` (`.claude/launch.json`, только 127.0.0.1, с автоперезагрузкой).
- База: SQLite `data/crm.db`. Таблицы создаются `create_all` при старте; **Alembic подключить до боевой базы** (шаг 8).
- Тестовые входы: `docs/test-users.md`. Сброс базы: удалить `data/crm.db`.
- Тесты: `.venv\Scripts\python -m pytest -q` (на временной базе).
- CSS/JS в шаблонах подключать через `{{ asset('/static/...') }}` — метка версии против кэша iPhone.
- Docker Compose + Caddy + PostgreSQL в контейнерах — шаг 8.

## Удалённый доступ (брат, показ)
- Tailscale на ПК Стаса (`desktop-edl1j4f`, 100.95.31.64). Включён **Tailscale Serve**:
  `https://desktop-edl1j4f.tail786720.ts.net` → `http://127.0.0.1:8810`. Только для участников сети Tailscale.
- Запасной вход **по IP, без DNS**: `http://100.95.31.64:8811` (Serve TCP 8811 → 127.0.0.1:8810).
  Нужен, когда у гостя Windows не берёт DNS Tailscale (так было у брата). Выключить: `tailscale serve --tcp=8811 off`.
- Брандмауэр Windows не трогали: Serve ходит на 127.0.0.1, правило не нужно.
- Работает, пока на ПК запущен `run.bat`. Выключить Serve: `tailscale serve reset`.

## Публичное демо (Render)
- Репозиторий открытый: github.com/AsaevDP/crm-sto. Демо разворачивается по `render.yaml`.
- Адрес демо: **https://crm-sto-demo.onrender.com** (Render, Blueprint `crm-sto`).
- Сам после push НЕ обновляется (сервис создан по ссылке на открытый репо, без GitHub-подключения).
- **Заливать только через `bash scripts/push.sh`** — он делает `git push` и сразу дёргает Deploy Hook
  Render (ссылка в `.env` → `RENDER_DEPLOY_HOOK`, в GitHub не коммитить!). Брат видит изменения через 3–5 мин.
- Запасной путь: `.github/workflows/deploy-demo.yml` — сработает при любом push, если Стас добавит
  секрет репозитория `RENDER_DEPLOY_HOOK`. Вручную: Render → crm-sto-demo → Manual Deploy → Deploy latest commit.
- `DEMO=1`: пароль всех тестовых сотрудников = `DEMO_PASSWORD` (генерирует Render), PIN и карт нет, плашка «Демо».
  Тестовые пароли из `seed.py` в демо НЕ работают — они публичны.
- Фото/логотипы на бесплатном Render не сохраняются (диск временный), база сбрасывается при перезапуске.

## Как устроено
- `audit.py` — журнал пишется сам при каждом сохранении; там же проставляются `created_by` и `branch_id`.
- `permissions.py` — справочник прав, роли по умолчанию, меню. Новый раздел = строка в `MENU` + право в `PERMISSIONS`.
- `web.py` — `Ctx` (текущий пользователь, права, `ctx.need(...)`, `ctx.render(...)`). Цвет роли → `--accent` в `<html style>`.

## Структура репозитория
```
CRMSTO/
├─ BRIEF.md, CLAUDE.md
├─ docs/                 схема БД, статусы, страница стиля
├─ app/
│  ├─ main.py            FastAPI-приложение
│  ├─ config.py          настройки из .env
│  ├─ db.py              подключение к БД
│  ├─ models/            таблицы (SQLAlchemy)
│  ├─ routes/            экраны по разделам: orders, stock, money, staff, settings…
│  ├─ services/          бизнес-логика: статусы, остатки, зарплата, нумерация
│  ├─ permissions.py     права доступа
│  ├─ templates/         Jinja2-шаблоны (HTMX)
│  └─ static/            css, js (htmx, alpine), иконки — всё локально
├─ migrations/           Alembic
├─ media/                фото (в .gitignore)
├─ tests/
├─ deploy/               docker-compose, Caddyfile, бэкапы (шаг 8)
└─ run.bat               запуск dev-сервера
```
