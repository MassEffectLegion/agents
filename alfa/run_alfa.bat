@echo off
setlocal
set AGENT_SLOT=0
set "AGENT_TASK_FILE=%~1"
if "%AGENT_TASK_FILE%"=="" set "AGENT_TASK_FILE=%~dp0task.txt"
set "AGENT_EXTRA_DIRS=<AGENTS_DIR>"  REM aec3a619 (GO человека 12.10, rest#33): доступ ко всему дереву агентов (замена 4 workdir-подкаталога; path_guard остаётся: H:\Windows* и пр. закрыты)
set "AGENT_LOG_FILE=%~dp0logs\alfa.txt"
if not defined AGENT_PYTHON set "AGENT_PYTHON=python"
if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"

REM ===== TG poller alfa (rest#8, GO человека HR c8cde3ea): фоновый queue-mode, каждые 5 мин, detached =====
REM Автоэскалация угроз в human_request + validate + авто-отказ. НЕ getUpdates (bravo = ingest).
REM "%AGENT_PYTHON%" "<AGENTS_DIR>\workdir\tg-bridge\alfa\tg_poller_launch_alfa.py" >nul 2>&1  REM F2   (d3ebc6b3 09.10: TG-мост = отдельная сущность, запуск только start_tg_min.bat)

REM ===== AUTO-VERIFY (rest#44, замыкает GO-обещание HR 6c66942a): после ЛЮБОГО запуска =====
REM Отдельный detached-консоль (start /min), НЕ блокирует main.py. Вывод: logs\post_restart_verify.out.
REM Откат: run_alfa.bat.bak_pre_r44verify.
start "" /min "%~dp0post_restart_verify_launch.bat"

REM ===== sleep watchdog alfa (rest#13, 61b9e820 P0): persistent sleep guard, dedup inside script; rollback: run_alfa.bat.bak_pre_r13 =====
start "" /min "%AGENT_PYTHON%" -u "%~dp0sleep_watchdog_alfa.py"
"%AGENT_PYTHON%" "%~dp0main.py"
REM 10c999cc: pause удалён (sleep_waiter.py)

