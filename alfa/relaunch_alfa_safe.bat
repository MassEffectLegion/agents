@echo off
REM alfa safe-relaunch (rest#17). УБИВАЕТ ТОЛЬКО alfa main.py (по command-line),
REM НИКАКИХ taskkill /f /im python.exe (убило бы bravo/db_service/tg-bridge!).
REM Args: /FORCE = агрессивный (если сплю — удалить запись сна и всё равно запустить).
setlocal
cd /d <AGENTS_DIR>\main_workdir\alfa
set LOG=logs\relaunch_safe_%date:~6,4%%date:~3,2%%date:~0,2%.log
set LOG=logs\relaunch_safe_%time:~0,2%%time:~3,2%%time:~6,2%_%random%.log

echo [%date% %time%] relaunch_safe started (arg=%1) > "%LOG%"

REM 1) alfa main.py жив?
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | Where-Object { $_.CommandLine -like '*main_workdir\alfa\main.py*' } | ForEach-Object { exit 1 }" 
if %errorlevel%==1 (
  echo [%date% %time%] alfa main.py ALIVE - noop >> "%LOG%"
  exit /b 0
)

REM 2) активная запись сна?
powershell -NoProfile -Command "$c='python'; & $c -c \"import json,psycopg2;c=psycopg2.connect(host='127.0.0.1',port=5432,dbname='agents',user='agents',password="CHANGE_ME");r=c.cursor();r.execute(\\\"SELECT wake_at FROM public.agent_sleeps WHERE agent_name='alfa'\\\");x=r.fetchone();print('SLEEPING',x[0] if x else '')\"" > "%LOG%.sleep" 2>&1
findstr /C:"SLEEPING" "%LOG%.sleep" >nul
if not errorlevel 1 (
  if /i "%~1"=="/FORCE" (
    powershell -NoProfile -Command "& 'python' -c \"import psycopg2;c=psycopg2.connect(host='127.0.0.1',port=5432,dbname='agents',user='agents',password="CHANGE_ME");c.cursor().execute(\\\"DELETE FROM public.agent_sleeps WHERE agent_name='alfa'\\\");c.commit();c.close()\"" >> "%LOG%" 2>&1
    echo [%date% %time%] SLEEP record deleted (FORCE) >> "%LOG%"
  ) else (
    echo [%date% %time%] alfa SLEEPING (sleep_watch will wake) - noop >> "%LOG%"
    exit /b 0
  )
)

REM 3) kill alfa main.py (точные PIDs)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | Where-Object { $_.CommandLine -like '*main_workdir\alfa\main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; 'KILLED ' + $_.ProcessId }" >> "%LOG%" 2>&1
timeout /t 5 /nobreak >nul

REM 4) relaunch
start "" cmd /c "<AGENTS_DIR>\main_workdir\alfa\run_alfa.bat"
echo [%date% %time%] RELAUNCHED >> "%LOG%"
exit /b 0
