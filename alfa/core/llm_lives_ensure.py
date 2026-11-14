# -*- coding: utf-8 -*-
"""llm_lives_ensure (e61f4b08 s3, alfa): поднять llm_lives_probe при старте агента, БЕЗ дублей.

Паттерн — diegle_ensure.py (294ad76a):
- find process: python ... llm_lives_probe\\llm_lives_probe.py (powershell Win32_Process).
- ЕСТЬ  -> НЕ создавать (dedup), status='already'.
- НЕТ   -> запуск через SHARED launch_detached.py (он сам: probe + detached + 15s health).
- LOCKFILE: sharedirs/llm_lives_probe/.launch.lock (TTL 10s) — гонка alfa+bravo.
- ЧИТАЕТ agent_settings.llm_lives_targets: ключа нет / enabled=false -> 'skipped'
  (быстро, без скана). Сам probe тоже перечитывает настройки при прогоне.
- IDEMPOTENT + NON-FATAL: любая ошибка -> log + {'status':'error'}, bootstrap НЕ падает.
- Запуск агента (alfa ИЛИ bravo) = единственный путь подъёма (cron не нужен).
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = r"<AGENTS_DIR>\workdir\sharedirs\llm_lives_probe"
LAUNCHER = os.path.join(BASE, "launch_detached.py")
LOCK = os.path.join(BASE, ".launch.lock")
LOCK_TTL_SEC = 10
PROBE_TIMEOUT = 30
LAUNCH_TIMEOUT = 90  # launch_detached.py: probe ~5s + sleep 15s + запас


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[llm_lives_ensure {ts}] {msg}", flush=True)
    try:
        with open(os.path.join(BASE, "ensure.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _settings_enabled() -> tuple:
    """(enabled: bool|None, detail). None — не удалось прочитать (продолжаем, probe сам решит)."""
    try:
        import psycopg2
        cfg = {"host": "127.0.0.1", "port": 5432, "database": "agents",
               "user": "agents", "password": "CHANGE_ME", "connect_timeout": 5, 'options': '-c search_path=agents_main,public'}
        try:
            sys.path.insert(0, r"<AGENTS_DIR>")
            from alfa.core.config import PG_CONFIG  # noqa: WPS433
            cfg = dict(PG_CONFIG)
        except Exception:
            pass
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()
        cur.execute("SELECT value FROM agent_settings WHERE setting_key='llm_lives_targets'")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None, "agent_settings.llm_lives_targets not found (probe default: enabled)"
        val = row[0]
        if isinstance(val, dict):
            return bool(val.get("enabled", True)), "enabled=%s" % val.get("enabled")
        return None, f"unexpected value type {type(val).__name__}"
    except Exception as e:
        return None, f"settings read error: {type(e).__name__}: {e}"


def _probe_process_line():
    """Командная строка живого llm_lives_probe.py или None."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            text=True, errors="ignore", timeout=PROBE_TIMEOUT)
        for line in out.splitlines():
            if "llm_lives_probe" in line and "llm_lives_probe.py" in line:
                return line.strip()
    except Exception as e:
        _log(f"probe warn: {type(e).__name__}: {e}")
    return None


def _acquire_lock(agent: str) -> bool:
    try:
        if os.path.exists(LOCK):
            age = time.time() - os.path.getmtime(LOCK)
            if age < LOCK_TTL_SEC:
                return False
            _log(f"stale lock (age={age:.0f}s) stolen from {agent}")
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(f"{agent}\t{datetime.now(timezone.utc).isoformat()}\t{os.getpid()}\n")
        return True
    except Exception as e:
        _log(f"lock error (continue anyway): {e}")
        return True


def _release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


def ensure_llm_lives(agent: str = "alfa", launch: bool = True) -> dict:
    """Поднять llm_lives_probe, если его нет. Возвращает {status, detail}.
    status: 'skipped' (disabled в agent_settings) | 'already' | 'launched' | 'error' | 'no_launcher'."""
    if not launch:
        return {"status": "skipped", "detail": "launch=False"}
    enabled, detail = _settings_enabled()
    if enabled is False:
        _log(f"SKIPPED: llm_lives_targets disabled ({detail})")
        return {"status": "skipped", "detail": f"disabled in agent_settings ({detail})"}
    if not os.path.exists(LAUNCHER):
        return {"status": "no_launcher", "detail": LAUNCHER}
    try:
        cur = _probe_process_line()
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
    res = ensure_llm_lives(agent="manual")
    print(res)
    sys.exit(0 if res["status"] in ("already", "launched", "skipped") else 1)
