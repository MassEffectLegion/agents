@echo off
setlocal
set "AGENT_TASK_FILE=%~1"
if "%AGENT_TASK_FILE%"=="" set "AGENT_TASK_FILE=%~dp0task.txt"
if not defined OMEGA_PYTHON set "OMEGA_PYTHON=%~dp0runtime\python.exe"
if not exist "%OMEGA_PYTHON%" (
  echo Omega Python is missing. Restore runtime or set OMEGA_PYTHON.
  exit /b 1
)
"%OMEGA_PYTHON%" -I -X utf8 "%~dp0boot.py" "%~dp0main.py"
pause
