

@echo off



REM ===== RESERVE MACHINE TARGET (269fc17f, GO 2026-10-28) =====

REM runs against llama-server 127.0.0.1:8282/v1, BGE 127.0.0.1:9090, Postgres 127.0.0.1:5432

setlocal

set AGENT_SLOT=0

set "AGENT_TASK_FILE=%~1"

if "%AGENT_TASK_FILE%"=="" set "AGENT_TASK_FILE=%~dp0task.txt"

set "AGENT_EXTRA_DIRS=<AGENTS_DIR>"  REM aec3a619 (GO человека 12.10, rest#33): доступ ко всему дереву агентов (замена 4 workdir-подкаталога; path_guard остаётся: H:\Windows* и пр. закрыты)

set "AGENT_LOG_FILE=%~dp0logs\delta2.txt"

REM c892328b (29.10, alfa): БД ТОЛЬКО через db_service bravo :8888 (db_api_*).
REM Fallback на raw SQL (query_postgres_local) — только если :8888 UNREACHABLE.
set "DB_SERVICE_URL=http://127.0.0.1:8888"

if not defined AGENT_PYTHON set "AGENT_PYTHON=python"

if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"



REM ===== TG poller delta2 (rest#8, GO человека HR c8cde3ea): фоновый queue-mode, каждые 5 мин, detached =====

REM Автоэскалация угроз в human_request + validate + авто-отказ. НЕ getUpdates (bravo = ingest).

REM "%AGENT_PYTHON%" "<AGENTS_DIR>\workdir\tg-bridge\delta2\tg_poller_launch_delta2.py" >nul 2>&1  REM F2   (d3ebc6b3 09.10: TG-мост = отдельная сущность, запуск только start_tg_min.bat)



REM ===== AUTO-VERIFY (rest#44, замыкает GO-обещание HR 6c66942a): после ЛЮБОГО запуска =====

REM Отдельный detached-консоль (start /min), НЕ блокирует main.py. Вывод: logs\post_restart_verify.out.

REM Откат: run_delta2.bat.bak_pre_r44verify.

if exist "%~dp0post_restart_verify_launch.bat" start "" /min "%~dp0post_restart_verify_launch.bat"



"%AGENT_PYTHON%" "%~dp0main.py"

pause