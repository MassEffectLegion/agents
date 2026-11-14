# -*- coding: utf-8 -*-
"""sleep_watchdog_gemma.py — MERGE c17 (gemma 08.11 19:35 МСК).
База: sleep_watchdog_gemma_ready.py (alfa rest#33, mailbox id=78: CONFIG, PROC_MARKERS сужен,
STALE-kill по CreationDate) + live c16 (anti-double lock) + исправления gemma c17:

G1 (CRITICAL): schema-квалификация — agents_main.lives / agents_main.tasks.
    ready-версия писала UPDATE lives/tasks БЕЗ схемы -> попадала в пустые public.lives/
    public.tasks (0 rows) = якорь живости и tasks.status='running' МОЛЧНО НЕ РАБОТАЛИ.
    Данные (lives gemma/alfa/bravo) живут в agents_main.* (проверено 08.11).
G2 (CRITICAL): anti-double lock (live c16) ВЕРНУТ (в ready его не было) + F1:
    живость PID в lock — через CIM (alive_check), НЕ os.kill(pid,0):
    на Windows os.kill(,0)=CTRL_C_EVENT/TERM — слал сигнал чужому консольному процессу.
G3: kill_main_py — taskkill /T (дерево: venv-parent + python311 child; twin = 2 CIM-строки).
Остальное = alfa ready: PROC_MARKERS=["gemma\\main.py"] (без ложного 'main_workdir'),
STALE-kill по маркеру fe455999_process_marker (маркера нет -> НЕ убиваем, консервативно),
_poll 300s, якорь SLEEPING.
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

# ===== CONFIG (alfa rest#33 + gemma c17) =====
AGENT      = "gemma"
PKG_ROOT   = Path(r"<AGENTS_DIR>\main_workdir\gemma")
RUN_BAT    = PKG_ROOT / "run_gemma.bat"
PY         = r"python"   # если свой venv — поправить
POLL_SEC   = 300
PROC_MARKERS = [r"gemma\main.py"]                    # ТОЛЬКО этот (не "main_workdir"!)
DB = dict(host="127.0.0.1", port=5432, dbname="agents", user="agents", password="CHANGE_ME")
LOG    = PKG_ROOT / "logs" / "sleep_watchdog_gemma.log"
LOCK   = PKG_ROOT / "logs" / "sleep_watchdog_gemma.lock"
MARKER = PKG_ROOT / "logs" / "fe455999_process_marker"  # sleep-guard-маркер (fe455999, Q3)
LOCK_TTL_SEC = 15 * 60
# ==================================================================

def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

def alive_check(pid):
    """F1: безопасная проверка живости PID (CIM). НЕ os.kill(,0) — на Windows это сигнал!"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \\'ProcessId=%d\\' | "
             "Select-Object ProcessId | ConvertTo-Json -Compress" % int(pid)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace").stdout.strip()
        return bool(out) and out != "null"
    except Exception:
        return None  # не уверены -> считаем живым (консервативно)

def _ps():
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
         "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=60, encoding="utf-8",
        errors="replace").stdout.strip()

def _parse():
    out = _ps()
    if not out or out == "null":
        return []
    try:
        d = json.loads(out)
        if isinstance(d, dict):
            d = [d]
        return d
    except Exception as e:
        log(f"parse ERROR: {e}: {out[:200]}")
        return []

def _is_ours(cl):
    cl = (cl or "").lower()
    if "watchdog" in cl or "monitor" in cl:
        return False
    return any(m.lower() in cl for m in PROC_MARKERS) and "main.py" in cl

def main_py_pids() -> list:
    return [x["ProcessId"] for x in _parse() if _is_ours(x.get("CommandLine"))]

def main_py_alive() -> bool:
    return bool(main_py_pids())

def main_py_is_old() -> bool:
    """True: живой main.py стартовал ДО маркера (не знает sleep-guard). Маркера нет -> False."""
    if not MARKER.exists():
        return False
    try:
        marker_ts = datetime.fromisoformat(MARKER.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    for x in _parse():
        if not _is_ours(x.get("CommandLine")):
            continue
        try:
            cd = datetime.strptime(x["CreationDate"], "%m/%d/%Y %H:%M:%S %p").replace(tzinfo=timezone.utc)
            if cd >= marker_ts:
                return False
        except Exception as e:
            log(f"old-check ERROR: {e}")
    return True

def launch_agent() -> str:
    if not RUN_BAT.exists():
        return f"NO-BAT: {RUN_BAT} отсутствует"
    subprocess.Popen([str(RUN_BAT)], creationflags=0x08000000 | 0x00000008,
                     cwd=str(PKG_ROOT), close_fds=True)
    return f"launched: {RUN_BAT.name}"

def kill_main_py():
    """G3: taskkill /T (дерево: venv-parent + python311 child)."""
    for pid in main_py_pids():
        try:
            subprocess.run(["taskkill", "/f", "/t", "/pid", str(pid)],
                           capture_output=True, timeout=30,
                           encoding="utf-8", errors="replace")
            log(f"killed main.py pid={pid} (tree)")
        except Exception as e:
            log(f"kill {pid} ERROR: {e}")

def check_once() -> str:
    import psycopg2
    conn = psycopg2.connect(connect_timeout=10, **DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, (wake_at AT TIME ZONE 'UTC'), need_work FROM public.agent_sleeps "
                        f"WHERE agent_name='{AGENT}' FOR UPDATE")
            row = cur.fetchone()
        if row is None:
            if main_py_alive():
                return "alive"
            return f"RELAUNCH (no-sleep, main.py was dead): {launch_agent()}"
        reason, wake_at, nw = row
        now = datetime.now(timezone.utc)
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        if now < wake_at and not nw:
            return "sleeping-ok (не трогаю)"
        # время подъёма
        if not main_py_alive():
            r = launch_agent()
            _finish_wake(conn, "watchdog-gemma")
            return f"wake-launched (main.py was dead): {r}"
        if main_py_is_old():
            kill_main_py()
            time.sleep(5)
            r = launch_agent()
            _finish_wake(conn, "watchdog-gemma:old-process-reboot")
            return f"wake-reboot-old-process: {r}"
        return "awake-wait (новый процесс жив — его sleep_watch обработает)"
    finally:
        conn.close()

def _finish_wake(conn, tag):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                    "actual_wake_at, need_work, woken_by, sleep_kind) "
                    f"SELECT '{AGENT}', reason, sleep_at, wake_at, now(), need_work, %s, sleep_kind "
                    "FROM public.agent_sleeps WHERE agent_name=%s", (tag, AGENT))
        cur.execute(f"DELETE FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
        # G1: agents_main.tasks (data живут там; public.tasks пуст)
        cur.execute(f"UPDATE agents_main.tasks SET status='running' WHERE current_agent='{AGENT}' AND status='sleeping'")
    conn.commit()

def keep_alive_anchor():
    """Пока gemma спит — держать agents_main.lives свежим (SLEEPING), чтобы alfa не эскалировал."""
    try:
        import psycopg2
        c = psycopg2.connect(connect_timeout=10, **DB)
        cur = c.cursor()
        cur.execute(f"SELECT wake_at FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
        row = cur.fetchone()
        if row:
            # G1: agents_main.lives (якорь живости читается напарниками из agents_main)
            cur.execute("UPDATE agents_main.lives SET last_heartbeat_at=NOW(), note=%s WHERE agent_name=%s",
                        (f"SLEEPING until {row[0]} (watchdog-gemma: alive)", AGENT))
            c.commit()
        c.close()
    except Exception as e:
        log(f"anchor ERROR: {type(e).__name__}: {e}")

def _acquire_lock():
    """G2: анти-двойной watchdog. O_EXCL + TTL; живость PID через alive_check (F1)."""
    try:
        if LOCK.exists():
            age = time.time() - LOCK.stat().st_mtime
            if age < LOCK_TTL_SEC:
                try:
                    d = json.loads(LOCK.read_text(encoding="utf-8"))
                    pid = d.get("pid")
                    if pid and alive_check(int(pid)):
                        log(f"ALREADY-RUNNING pid={pid} age={int(age)}s (exit)")
                        return False
                except Exception:
                    pass
            else:
                log(f"STALE lock (age {int(age)}s > TTL) — снимаю")
                LOCK.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    except FileExistsError:
        log("ALREADY-RUNNING (O_EXCL race) — exit")
        return False
    except Exception:
        pass
    return True

if __name__ == "__main__":
    if not _acquire_lock():
        sys.exit(0)
    log(f"START pid={os.getpid()} agent={AGENT} root={PKG_ROOT} bat={RUN_BAT} markers={PROC_MARKERS} (merge c17)")
    if not main_py_alive():
        log("first-poll: main.py dead right now -> will relaunch next tick")
    while True:
        try:
            r = check_once()
            log(f"check: {r}")
            keep_alive_anchor()
        except Exception as e:
            log(f"check ERROR: {type(e).__name__}: {e}")
        time.sleep(POLL_SEC)
