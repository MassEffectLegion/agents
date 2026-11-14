@echo off
REM ============================================================
REM d68e080c (bravo, 29.10): ЗАПУСК 2 LLM-серверов БОНСАЙ для delta1/delta2
REM ВАРИАНТ B (CPU, 1.77): Ternary-Bonsai-2-27B-PQ2_0 -ngl 0
REM   delta1 -> 127.0.0.1:8280   delta2 -> 127.0.0.1:8281
REM Применять ТОЛЬКО после GO alfa (Q2=B) + человек включает дельт.
REM ВАРИАНТ A: этот файл НЕ нужен (3.55, runtime_defaults не менять).
REM ВАРИАНТ C: delta1 -> 3.55:8080 (runtime_defaults delta1 правится отдельно).
REM ============================================================
REM Перед запуском задайте: set MODEL_PATH=C:\llamaModel  set LLAMA_DIR=C:\llama3
setlocal
set MODEL=%MODEL_PATH%\Ternary-Bonsai-2-27B-PQ2_0.gguf
set EXE=%LLAMA_DIR%\llama-server.exe
if not exist "%EXE%" (echo NO llama-server.exe at %EXE% & exit /b 1)
if not exist "%MODEL%" (echo NO MODEL %MODEL% & exit /b 1)

REM preflight: порты свободны?
powershell -NoProfile -Command "
$ok=$true
foreach($p in 8280,8281){
  $c=New-Object Net.Sockets.TcpClient
  try { $c.Connect('127.0.0.1',$p); Write-Host 'PORT $p BUSY - abort'; exit 1 } catch {}
  finally { $c.Close() }
}
exit 0" || exit /b 1

REM backup runtime_defaults BEFORE edit
copy /Y "<AGENTS_DIR>\main_workdir\delta1\runtime_defaults.json" "<AGENTS_DIR>\main_workdir\delta1\runtime_defaults.json.bak_d68e080c_2910" >nul
copy /Y "<AGENTS_DIR>\main_workdir\delta2\runtime_defaults.json" "<AGENTS_DIR>\main_workdir\delta2\runtime_defaults.json.bak_d68e080c_2910" >nul

REM edit llama_server_url (1.77 local: 8280/8281)
powershell -NoProfile -Command "
$p1='<AGENTS_DIR>\main_workdir\delta1\runtime_defaults.json'
$j1=Get-Content $p1 -Raw | ConvertFrom-Json
$j1.llama_server_url='http://127.0.0.1:8280/v1'
$j1 | ConvertTo-Json -Depth 5 | Set-Content $p1 -Encoding UTF8
$p2='<AGENTS_DIR>\main_workdir\delta2\runtime_defaults.json'
$j2=Get-Content $p2 -Raw | ConvertFrom-Json
$j2.llama_server_url='http://127.0.0.1:8281/v1'
$j2 | ConvertTo-Json -Depth 5 | Set-Content $p2 -Encoding UTF8
Write-Host 'runtime_defaults updated: delta1=127.0.0.1:8280 delta2=127.0.0.1:8281'
"

REM launch 2 detached CPU servers (PQ2_0 ~7.2GB RAM each, RAM на 1.77 ~66GB free)
start "bonsai_delta1_8280" /min %EXE% -m "%MODEL%" -ngl 0 -c 16384 --host 127.0.0.1 --port 8280 -b 4096 -ub 4096 --parallel 1
start "bonsai_delta2_8281" /min %EXE% -m "%MODEL%" -ngl 0 -c 16384 --host 127.0.0.1 --port 8281 -b 4096 -ub 4096 --parallel 1

REM wait + verify
timeout /t 45 /nobreak >nul
echo === VERIFY ===
curl -s -m 5 http://127.0.0.1:8280/v1/models | findstr /i "bonsai" >nul && echo 8280 OK || echo 8280 FAIL
curl -s -m 5 http://127.0.0.1:8281/v1/models | findstr /i "bonsai" >nul && echo 8281 OK || echo 8281 FAIL
echo Done. ОТПРАТЬ: human_request (запуск бонсай CPU 8280/8281 для дельт) + agent_coordination inbox:alfa.
