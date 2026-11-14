@echo off
REM rest#44: detached post-restart verify (new pid + live-RAG + stale-tools audit).
REM Called by run_gemma.bat via: start "" /min "%~dp0post_restart_verify_launch.bat"
REM Output: logs\post_restart_verify.out. Best-effort — never blocks main.py.
set "AGENT_PYTHON=python"
if not exist "%AGENT_PYTHON%" set "AGENT_PYTHON=python"
"%AGENT_PYTHON%" "%~dp0_r43_post_restart_verify.py" > "%~dp0logs\post_restart_verify.out" 2>&1
