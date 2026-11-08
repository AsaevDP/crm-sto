@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Первый запуск: создаём окружение и ставим библиотеки
if not exist ".venv\Scripts\python.exe" (
    echo Устанавливаю окружение, это один раз...
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :err
)

echo.
echo  CRM СТО запущена
echo  На этом ПК:      http://localhost:8810
echo  С iPhone по Wi-Fi: http://^<IP этого ПК^>:8810   (IP смотреть: ipconfig)
echo  Остановить: закрыть это окно
echo.
start "" http://localhost:8810
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8810
goto :eof

:err
echo Ошибка установки. Проверьте, что установлен Python 3.11+
pause
