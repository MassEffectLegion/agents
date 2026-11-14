@echo off
setlocal
REM ===== c221 guard-launched bravo (mirror of run_bravo.bat, NO pause, guard-safe) =====
set "AGENT_SLOT=0"
set "AGENT_EXTRA_DIRS=<AGENTS_DIR>"
set "AGENT_LOG_FILE=<AGENTS_DIR>\main_workdir\bravo\logs\bravo.txt"
if not defined AGENT_PYTHON set "AGENT_PYTHON=python"
if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"
"%AGENT_PYTHON%" "<AGENTS_DIR>\main_workdir\bravo\main.py"
