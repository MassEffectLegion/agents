@echo off
setlocal

REM ===== Agent Bravo: main mode =====
REM Usage: run_bravo.bat [task.txt]  — direct run
REM Usage: run_bravo.bat --sleep     -- sleep mode daemon

set "AGENT_TASK_FILE=%~1"
if "%AGENT_TASK_FILE%"=="--sleep" goto sleep_mode

set AGENT_SLOT=0
if "%AGENT_TASK_FILE%"=="" set "AGENT_TASK_FILE=%~dp0task.txt"
set "AGENT_EXTRA_DIRS=<AGENTS_DIR>"  REM aec3a619 (GO человека 12.10, rest#33): доступ ко всему дереву агентов (замена 4 workdir-подкаталогов; path_guard остаётся: H:\Windows* и пр. закрыты)
set "AGENT_LOG_FILE=%~dp0logs\bravo.txt"
if not defined AGENT_PYTHON set "AGENT_PYTHON=python"
if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"

REM ===== TG poller (rest#42, GO HR 020d7649): фоновый, каждые 5 мин, detached =====
REM "%AGENT_PYTHON%" "<AGENTS_DIR>\workdir\tg-bridge\bravo\tg_poller_launch.py" >nul 2>&1  REM F2   (d3ebc6b3 09.10: TG-мост = отдельная сущность, запуск только start_tg_min.bat)

"%AGENT_PYTHON%" "%~dp0main.py"
pause
goto :end

REM ===== Sleep mode =====
:sleep_mode
echo Starting sleep mode daemon...
if not defined AGENT_PYTHON set "AGENT_PYTHON=python"
if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"

REM ===== TG poller (rest#42, GO HR 020d7649): фоновый, каждые 5 мин, detached =====
REM "%AGENT_PYTHON%" "<AGENTS_DIR>\workdir\tg-bridge\bravo\tg_poller_launch.py" >nul 2>&1  REM F2   (d3ebc6b3 09.10: TG-мост = отдельная сущность, запуск только start_tg_min.bat)

"%AGENT_PYTHON%" "%~dp0workspace\c471_migrated_root\sleep_mode_daemon.py"
pause

:end
