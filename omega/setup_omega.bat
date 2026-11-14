@echo off
setlocal
if not defined OMEGA_PYTHON set "OMEGA_PYTHON=%~dp0runtime\python.exe"
"%OMEGA_PYTHON%" -I -X utf8 "%~dp0install_offline.py"
if errorlevel 1 exit /b 1
"%OMEGA_PYTHON%" -I -X utf8 "%~dp0boot.py" "%~dp0manage.py" check
pause
