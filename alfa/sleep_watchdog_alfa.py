# -*- coding: utf-8 -*-
"""alfa sleep-watchdog v2 (rest#17): ПЕРСИСТЕНТНЫЙ демон (детаченый, живёт независимо от main.py).
Резерв для sleep_watch (который живёт в процессе main.py и умирает вместе с ним, если процесс
не перезапускается). Логика (опрос 5 мин):
 - нет записи сна -> no-sleep
 - now < wake_at и !need_work -> sleeping-ok
 - время подъёма:
   * main.py МЁРТВ -> launch run_alfa.bat
   * main.py жив, НО старый (старт ДО маркера fe455999_process_marker) -> kill + launch
     (старый процесс не знает sleep-guard/sleep_watch)
   * main.py жив и НОВЫЙ -> НЕ ТРОГАТЬ (у него свой sleep_watch; запись НЕ удаляем —
     его sleep-guard/sleep_watch обработают)
Log: logs/sleep_watchdog_alfa.log. Запуск: python -u sleep_watchdog_alfa.py (детаченый).
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

AGENT = "alfa"
PKG_ROOT = Path(r"<AGENTS_DIR>\main_workdir\alfa")
LOG = PKG_ROOT / "logs" / "sleep_watchdog_alfa.log"
MARKER = PKG_ROOT / "logs" / "fe455999_process_marker"
PY = r"python"

def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

def _ps():
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
         "Where-Object { $_.CommandLine -like '*main_workdir\\alfa\\main.py*' } | "
         "Select-Object ProcessId,CreationDate | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=40).stdout.strip()

def main_py_pids() -> list:
    out = _ps()
    if not out or out == "null":
        return []
    try:
        d = json.loads(out)
        if isinstance(d, dict):
            d = [d]
        return [x["ProcessId"] for x in d]
    except Exception as e:
        log(f"parse ERROR: {e}: {out[:200]}")
        return []

def main_py_alive() -> bool:
    return bool(main_py_pids())

def main_py_is_old() -> bool:
    """True если живой main.py стартовал ДО маркера (не знает fe455999 код)."""
    if not MARKER.exists():
        return True
    marker_ts = datetime.fromisoformat(MARKER.read_text(encoding="utf-8").strip())
    out = _ps()
    if not out or out == "null":
        return False
    try:
        d = json.loads(out)
        if isinstance(d, dict):
            d = [d]
        for x in d:
            cd = datetime.strptime(x["CreationDate"], "%m/%d/%Y %H:%M:%S %p").replace(tzinfo=timezone.utc)
            if cd >= marker_ts:
                return False
        return True
    except Exception as e:
        log(f"old-check ERROR: {e}")
        return True  # не знаем -> консервативно (kill+launch при подъёме)

def launch_agent() -> str:
    bat = PKG_ROOT / f"run_{AGENT}.bat"
    subprocess.Popen([str(bat)], creationflags=0x08000000 | 0x00000008,
                     cwd=str(PKG_ROOT), close_fds=True)
    return "launched: run_alfa.bat"

def kill_main_py():
    for pid in main_py_pids():
        try:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, timeout=30)
            log(f"killed main.py pid={pid}")
        except Exception as e:
            log(f"kill {pid} ERROR: {e}")

def check_once() -> str:
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="agents",
                            user="agents", password="CHANGE_ME", connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, (wake_at AT TIME ZONE 'UTC'), need_work FROM public.agent_sleeps "
                        f"WHERE agent_name='{AGENT}' FOR UPDATE")
            row = cur.fetchone()
        if row is None:
            return "no-sleep"
        reason, wake_at, nw = row
        now = datetime.now(timezone.utc)
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        if now < wake_at and not nw:
            return "sleeping-ok"
        if not main_py_alive():
            launch_agent()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                            "actual_wake_at, need_work, woken_by, sleep_kind) "
                            f"SELECT '{AGENT}', reason, sleep_at, wake_at, now(), need_work, 'watchdog:rest17', sleep_kind "
                            f"FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
                cur.execute(f"DELETE FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
                cur.execute(f"UPDATE tasks SET status='running' WHERE current_agent='{AGENT}' AND status='sleeping'")
            conn.commit()
            return "wake-launched (main.py was dead)"
        if main_py_is_old():
            kill_main_py()
            time.sleep(5)
            launch_agent()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                            "actual_wake_at, need_work, woken_by, sleep_kind) "
                            f"SELECT '{AGENT}', reason, sleep_at, wake_at, now(), need_work, 'watchdog:rest17:old-process-reboot', sleep_kind "
                            f"FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
                cur.execute(f"DELETE FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
                cur.execute(f"UPDATE tasks SET status='running' WHERE current_agent='{AGENT}' AND status='sleeping'")
            conn.commit()
            return "wake-reboot-old-process"
        return "awake-wait (новый процесс жив — его sleep_watch обработает; запись НЕ трогаю)"
    finally:
        conn.close()

def _pid_alive(pid: int) -> bool:
    # Windows: OpenProcess (QUERY_ONLY). No os.kill (on Windows signal 0 hits the whole group).
    if pid <= 0:
        return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x0400, False, int(pid))  # PROCESS_QUERY_INFORMATION
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True

def already_running():
    # Dedup (rest#13, 61b9e820 P0): lockfile pid+ts; refresh each loop; stale if pid dead or ts>8min.
    # v4: NO powershell (its child CommandLine contains the pattern -> false-positive;
    # root cause of the 6-daemon pile-up, rest#12).
    lock = LOG.parent / "sleep_watchdog_alfa.lock"
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        pid, ts = int(data.get("pid", -1)), float(data.get("ts", 0.0))
        if time.time() - ts > 480:
            return False  # stale (loop=5min; >8min = dead daemon)
        return _pid_alive(pid)
    except Exception:
        return False      # no lock / unreadable -> free

if __name__ == "__main__":
    if not MARKER.exists():
        MARKER.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    if already_running():
        log("another watchdog alive -> exit (dedup rest#13)")
        sys.exit(0)
    (LOG.parent / "sleep_watchdog_alfa.lock").write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    log(f"START pid={os.getpid()} marker={MARKER.read_text(encoding='utf-8')}")
    def keep_alive_anchor():
        # lives-якорь: пока агент спит, watchdog держит heartbeat свежим + note=SLEEPING
        # (bravo не эскалирует: агент не мёртв, а спит до wake_at).
        try:
            import psycopg2
            c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="agents",
                                 user="agents", password="CHANGE_ME", connect_timeout=10)
            cur = c.cursor()
            cur.execute("SELECT wake_at FROM public.agent_sleeps WHERE agent_name='alfa'")
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE lives SET last_heartbeat_at=NOW(), note=%s WHERE agent_name='alfa'",
                            (f"SLEEPING until {str(row[0])} (watchdog-alfa: alive, fe455999)",))
                c.commit()
            c.close()
        except Exception as e:
            log(f"anchor ERROR: {type(e).__name__}: {e}")

    while True:
        try:
            r = check_once()
            if r != "sleeping-ok":
                log(f"check: {r}")
            keep_alive_anchor()
        except Exception as e:
            log(f"check ERROR: {type(e).__name__}: {e}")
        (LOG.parent / "sleep_watchdog_alfa.lock").write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        time.sleep(300)
