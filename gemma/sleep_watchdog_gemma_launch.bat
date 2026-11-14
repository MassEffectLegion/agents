@echo off
REM c16: detached-запуск gemma liveness-watchdog (шаблон alfa id=68 + anti-double lock).
REM Auto-start (ТРЕБУЕТ ГО человека, shell:startup / Task Scheduler):
REM   shell:startup:  cmd /c "echo @echo off > %APPDATA%\Microsoft\Windows\Start Menu\Programs\StartUp\gemma_watchdog.bat && echo start /min %~dp0sleep_watchdog_gemma.bat >> %APPDATA%\Microsoft\Windows\Start Menu\Programs\StartUp\gemma_watchdog.bat"
REM   (или Task Scheduler: trigger=Logon, action=этот bat, "Run whether user is logged on or not")
setlocal
set "PY=python"
if not exist "%PY%" set "PY=python"
start "" /min "%PY%" -u "%~dp0sleep_watchdog_gemma.py"
