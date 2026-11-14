@echo off
REM alfa relaunch (rest#17): kill old main.py (pre-fe455999 code) + start run_alfa.bat fresh.
REM Called by: (a) detached python right now, (b) schtasks one-shot ALFA_REL_0900 (backup).
REM Idempotent/safe: if no alfa main.py -> just launches a new one (2nd concurrent process
REM is harmless: it will block on the running task until the first exits - but we kill first).
setlocal
cd /d <AGENTS_DIR>\main_workdir\alfa
timeout /t 3 /nobreak >nul
REM kill existing alfa main.py (both venv + system python wrappers)
taskkill /f /im python.exe /fi "WINDOWTITLE ne *" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | Where-Object { $_.CommandLine -like '*main_workdir\alfa\main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 3 /nobreak >nul
REM relaunch (new console, detached from us)
start "" cmd /c "<AGENTS_DIR>\main_workdir\alfa\run_alfa.bat"
echo RELAUNCHED alfa at %date% %time% > "<AGENTS_DIR>\main_workdir\alfa\logs\relaunch_%random%.txt"
