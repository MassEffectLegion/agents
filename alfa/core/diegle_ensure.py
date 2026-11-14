# -*- coding: utf-8 -*-
"""diegle_ensure (294ad76a s2, alfa): поднять diegle-summary при старте агента, БЕЗ дублей.

Дизайн (plan/294ad76a_analysis_alfa.md):
- find process: python ... diegle_summary\\main.py (powershell Win32_Process).
- ЕСТЬ  -> НЕ создавать (dedup), вернуть status='already'.
- НЕТ   -> запуск через SHARED launch_detached.py (он сам: probe + detached + 15s health).
- LOCKFILE: sharedirs/diegle_summary/.launch.lock (TTL 10s) — защита от гонки
  alfa+bravo при синхронном старте (оба увидят «не запущен» и оба захотят запускнуть).
  TTL: stale-lock (агент упал между lock и launch) не блокирует вечно.
- IDEMPOTENT + NON-FATAL: любая ошибка -> log + return {'status':'error'}, bootstrap НЕ падает.
- Запуск агента (alfa ИЛИ bravo) = единственный путь подъёма демона
  (Task Scheduler diegle-summary будет Disabled на шаге s4).
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = r"<AGENTS_DIR>\workdir\sharedirs\diegle_summary"
LAUNCHER = os.path.join(BASE, "launch_detached.py")
LOCK = os.path.join(BASE, ".launch.lock")
LOCK_TTL_SEC = 10  # stale-lock TTL
PROBE_TIMEOUT = 30
LAUNCH_TIMEOUT = 120  # launch_detached.py: probe 30s + sleep 15s + запас


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[diegle_ensure {ts}] {msg}", flush=True)
    try:
        with open(os.path.join(BASE, "ensure.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _diegle_launchers():
    """Список ЛОГИЧЕСКИХ демонов: python-процессы с cmdline diegle_summary...main.py,
    чей РОДИТЕЛЬ НЕ является diegle-python (venv-launcher + base-python re-exec child
    дают два процесса с одним cmdline — child не считается отдельным демоном).
    Возврат: [command_line, ...]."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json"],
            text=True, errors="ignore", timeout=PROBE_TIMEOUT)
        import json
        procs = json.loads(out or "[]")
        if isinstance(procs, dict):
            procs = [procs]
        diegle_ids = set()
        for p in procs:
            cl = p.get("CommandLine") or ""
            if "diegle_summary" in cl and "main.py" in cl:
                diegle_ids.add(str(p.get("ProcessId")))
        launchers = []
        for p in procs:
            cl = p.get("CommandLine") or ""
            if "diegle_summary" in cl and "main.py" in cl \
               and str(p.get("ParentProcessId")) not in diegle_ids:
                launchers.append(cl.strip())
        return launchers
    except Exception as e:
        _log(f"probe warn: {type(e).__name__}: {e}")
        return []


def _diegle_process_line():
    """Вернёт командную строку живого diegle-демона (launcher) или None."""
    ls = _diegle_launchers()
    return ls[0] if ls else None


def _acquire_lock(agent: str) -> bool:
    """True — lock наш (или stolen stale); False — другой агент запускает прямо сейчас."""
    try:
        if os.path.exists(LOCK):
            age = time.time() - os.path.getmtime(LOCK)
            if age < LOCK_TTL_SEC:
                return False  # свежий lock — кто-то в процессе
            _log(f"stale lock (age={age:.0f}s) stolen from {agent}")
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(f"{agent}\t{datetime.now(timezone.utc).isoformat()}\t{os.getpid()}\n")
        return True
    except Exception as e:
        _log(f"lock error (continue anyway): {e}")
        return True  # не блокируем подъём из-за lock-проблемы (dedup в launcher'е 2-я линия)


def _release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


def ensure_diegle(agent: str = "alfa", launch: bool = True) -> dict:
    """Поднять diegle-summary, если его нет. Возвращает {status, detail}.
    status: 'already' (демон жив — дубль НЕ создан) | 'launched' | 'skipped' | 'error' | 'no_launcher'."""
    if not launch:
        return {"status": "skipped", "detail": "launch=False"}
    if not os.path.exists(LAUNCHER):
        return {"status": "no_launcher", "detail": LAUNCHER}
    try:
        cur = _diegle_process_line()
        if cur:
            _log(f"ALREADY RUNNING (dedup): {cur}")
            return {"status": "already", "detail": cur}
        if not _acquire_lock(agent):
            _log("fresh lock exists — другой агент запускает, пропускаю")
            return {"status": "skipped", "detail": "lock held by another agent (TTL 10s)"}
        try:
            out = subprocess.run(
                [sys.executable, LAUNCHER],
                capture_output=True, text=True, errors="ignore", timeout=LAUNCH_TIMEOUT)
            tail = (out.stdout or "")[-400:]
            if "STARTED" in (out.stdout or ""):
                _log(f"LAUNCHED: {tail}")
                return {"status": "launched", "detail": tail}
            if "ALREADY RUNNING" in (out.stdout or ""):
                _log(f"race resolved by launcher dedup: {tail}")
                return {"status": "already", "detail": tail}
            _log(f"launcher rc={out.returncode}: {tail} stderr={(out.stderr or '')[-200:]}")
            return {"status": "error", "detail": f"rc={out.returncode} {tail}"}
        finally:
            _release_lock()
    except subprocess.TimeoutExpired:
        _release_lock()
        return {"status": "error", "detail": f"launcher timeout {LAUNCH_TIMEOUT}s"}
    except Exception as e:
        _release_lock()
        _log(f"ensure error: {type(e).__name__}: {e}")
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    res = ensure_diegle(agent="manual")
    print(res)
    sys.exit(0 if res["status"] in ("already", "launched", "skipped") else 1)
