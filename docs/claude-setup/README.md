# Набор настройки Claude для нового ПК

Чтобы Claude работал «как у Стаса», а не «из коробки». Делается **один раз**, сам Claude:
открыть проект в Claude (вкладка Code) и написать:

> Настрой Claude на этом ПК по docs/claude-setup/README.md

## Что сделать Claude (по шагам, каждое — с разрешения хозяина ПК)
1. **Личные правила** — скопировать `docs/claude-setup/CLAUDE.md` в `%USERPROFILE%\.claude\CLAUDE.md`.
   Если файл уже есть — не затирать, а дописать недостающее.
2. **Навык дизайна** — скопировать папку `docs/claude-setup/skills/frontend-design`
   в `%USERPROFILE%\.claude\skills\frontend-design` (помогает делать аккуратный интерфейс).
3. **Меньше вопросов «разрешить?»** — создать в проекте `.claude/settings.local.json`
   (в Git не попадает) с разрешением на частые безопасные команды:
   ```json
   {
     "permissions": {
       "allow": [
         "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git pull:*)",
         "Bash(git add:*)", "Bash(git commit:*)",
         "Bash(.venv/Scripts/python:*)", "Bash(.venv/Scripts/python.exe:*)",
         "PowerShell(git status:*)", "PowerShell(git pull:*)",
         "PowerShell(.venv\\Scripts\\python:*)", "PowerShell(.venv\\Scripts\\python.exe:*)"
       ]
     }
   }
   ```
4. **Просмотр CRM в Claude** — если нет `.claude/launch.json`, создать (сервер `crmsto`, порт 8810):
   ```json
   {"version": "0.0.1", "configurations": [{"name": "crmsto",
     "runtimeExecutable": ".venv/Scripts/python.exe",
     "runtimeArgs": ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8810", "--reload"],
     "port": 8810}]}
   ```
   Перед первым запуском — один раз `run.bat` (создаёт `.venv` и ставит библиотеки).
5. **Проверка** — запустить CRM (`crmsto`), войти admin / admin123, показать скриншот.

## Что настроить руками в приложении Claude
- Вкладка **Code**, папка проекта `crm-sto`.
- Режим разрешений (внизу у поля ввода): **Auto** — Claude сам делает безопасное и спрашивает только важное.
- Уведомления на телефон (настройки приложения) — удобно, когда Claude долго работает.
