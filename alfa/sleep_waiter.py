# -*- coding: utf-8 -*-
"""alfa sleep-waiter v3 (GO человека 10c999cc, rest#22): ВИДИМАЯ консоль сна.
Заменяет bat-`pause` ("нажмите любую клавишу" -> консоль закрывалась).
Запускается main.py (sleep-guard) в НОВОМ видимом окне с заголовком "ALFA SLEEP до <wake_at>".
Логика:
  1) читает public.agent_sleeps (wake_at, need_work) — время сна уже в БД (метка человека);
  2) обновляет lives.note = "SLEEPING until <wake_at> (waiter-alfa v3)";
  3) цикл 30c: ping 127.0.0.1 (heartbeat-консоль) + строка "осталось HH:MM:SS";
  4) подъём (now>=wake_at ИЛИ need_work=true): log INSERT, DELETE записи,
     tasks sleeping->running, запуск run_alfa.bat (detached), exit;
  5) если main.py уже жив (двойной старт) — только cleanup записи, exit;
  6) dedup: lockfile logs/sleep_waiter_alfa.lock (pid+TTL 6 мин).
Log: logs/sleep_waiter_alfa.log. Резерв: sleep_watch/sleep_watchdog_alfa (не трогаем — у них своя дедуп).
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import os
AGENT = os.environ.get("LONGLIVE_AGENT_NAME") or os.environ.get("AGENT_NAME") or "alfa"
PKG_ROOT = Path(__file__).resolve().parent
LOG = PKG_ROOT / "logs" / "sleep_waiter_alfa.log"
POLL = 30  # сек

def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

def db():
    import psycopg2
    cfg = json.loads((PKG_ROOT / "runtime_settings.local.json").read_text(encoding="utf-8"))["postgres"]
    ALLOWED = ("host","port","database","user","password","options","connect_timeout","dbname","sslmode")
    c = {k: v for k, v in cfg.items() if k in ALLOWED}; c.setdefault("connect_timeout", 10); return psycopg2.connect(**c)

def main_py_alive() -> bool:
    try:
        out = subprocess.run(
            ["powershell","-NoProfile","-Command",
             "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
             "Where-Object { $_.CommandLine -like '*main_workdir\\\\alfa\\\\main.py*' -and $_.CommandLine -notlike '*sleep_waiter*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=40).stdout.strip()
        return bool(out)
    except Exception:
        return False

def main():
    lock = LOG.parent / "sleep_waiter_alfa.lock"
    try:
        d = json.loads(lock.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) < 360 and d.get("pid") != os.getpid():
            log("another waiter alive -> exit (dedup)")
            return
    except Exception:
        pass
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    log(f"START waiter pid={os.getpid()}")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, wake_at FROM public.agent_sleeps WHERE agent_name=%s", (AGENT,))
            row = cur.fetchone()
        if not row:
            log("no sleep record -> exit")
            return
        reason, wake_at = row
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        with conn.cursor() as cur:
            cur.execute("UPDATE lives SET note=%s WHERE agent_name=%s",
                        (f"SLEEPING until {wake_at.isoformat()} (waiter-alfa v3, {reason})", AGENT))
        conn.commit()
        print(f"=== ALFA СОН до {wake_at.strftime('%H:%M:%S UTC')} (reason={reason}) ===")
        print(f"Консоль-ожидатель: ping 127.0.0.1 каждые {POLL}c. Подъём: schedule/need_work. НЕ закрывать (можно).")
        while True:
            now = datetime.now(timezone.utc)
            left = (wake_at - now).total_seconds()
            nw = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT need_work FROM public.agent_sleeps WHERE agent_name=%s", (AGENT,))
                    r = cur.fetchone()
                    nw = bool(r and r[0]) if r else False
            except Exception as e:
                log(f"poll ERR: {e}")
            subprocess.run("ping -n 1 127.0.0.1", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if nw or left <= 0:
                log(f"WAKE (need_work={nw}, left={left:.0f}s)")
                print(f"\nПодъём: need_work={nw} -> запускаю main.py")
                # main.py уже жив? -> только cleanup
                if main_py_alive():
                    log("main.py already alive -> cleanup only")
                else:
                    subprocess.Popen(["cmd", "/c", str(PKG_ROOT / f"run_{AGENT}.bat")],
                                     cwd=str(PKG_ROOT), close_fds=True)
                    log("launched run_alfa.bat")
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, actual_wake_at, need_work, woken_by, sleep_kind) "
                                f"SELECT '{AGENT}', reason, sleep_at, wake_at, now(), %s, 'waiter-alfa-v3', sleep_kind FROM public.agent_sleeps WHERE agent_name='{AGENT}'", (nw,))
                    cur.execute("DELETE FROM public.agent_sleeps WHERE agent_name=%s", (AGENT,))
                    cur.execute("UPDATE tasks SET status='running' WHERE current_agent=%s AND status='sleeping'", (AGENT,))
                    cur.execute("UPDATE lives SET note=%s WHERE agent_name=%s", (f"WAKE (waiter-alfa v3)", AGENT))
                conn.commit()
                return
            mm, ss = divmod(int(max(0, left)), 60)
            hh, mm = divmod(mm, 60)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] сон... осталось {hh:02d}:{mm:02d}:{ss:02d}")
            time.sleep(POLL)
    finally:
        try: conn.close()
        except Exception: pass
        try: lock.unlink()
        except Exception: pass

if __name__ == "__main__":
    main()
