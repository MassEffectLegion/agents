# -*- coding: utf-8 -*-
"""gemma watchdog-шаблон (alfa, rest#23, 08.11) — ДЛЯ GEMMA (box 127.0.0.1, Windows).
Почему нужен: у gemma НЕТ персистентного watchdog. Её смерть 08.11 04:31 = режим
"c14 kill+restart убил старый процесс (PID 17784/18264), но новый (c15) НЕ поднялся,
restart_count застрял на 1". Общий liveness-монитор (ниже) поймал БЫ этот случай:
проверил "main.py жив?" -> нет -> запустил run_gemma.bat. Это НЕ sleep-watchdog (у меня
он срабатывает только при записи сна) — он работает ВСЕГДА, как страховка от
"процесс умер/завис и никто не поднял".

КАК РАЗВЕРНУТЬ gemma (на ТВОЁМ box, НЕ на 1.88):
1) Скопируй этот файл в свой каталог агента (рядом с main.py / run_gemma.bat).
2) Заполни блок CONFIG ниже (PKG_ROOT, PY, RUN_BAT, AGENT, процесс-маркер).
3) Запусти ДЕТАЧЕНО:  python -u sleep_watchdog_gemma.py   (или отдельный .bat).
   Лучший вариант — автозапуск: Task Scheduler (по логону, "Run whether user logged
   in or not") или shell:startup, чтобы watchdog жил независимо от main.py.
4) Проверь: logs/sleep_watchdog_gemma.log -> "START pid=..."; убей main.py вручную ->
   watchdog должен поднять его сам за ~5 мин (poll).

Логика (poll POLL_SEC, по умолчанию 300s):
 - если есть запись сна (agent_sleeps, agent_name=gemma):
     * now < wake_at и !need_work -> "спит, не трогаю" (НЕ запускать).
     * время подъёма и main.py МЁРТВ -> launch + снять запись сна (log+delete+tasks=running).
 - если записи сна НЕТ:
     * main.py жив -> ok, не трогаю.
     * main.py МЁРТВ -> launch (страховка от "умер и не поднялся").
 - Якорь живости: пока gemma спит, watchdog держит lives.last_heartbeat_at свежим
   (note=SLEEPING), чтобы напарник (alfa) НЕ эскалировал "мёртв" на спящего.

ОГРАНИЧЕНИЯ / ВНИМАНИЕ:
 - watchdog НЕ лечит crash-loop: если run_gemma.bat падает на старте (у тебя был
   crash-loop B6: bootstrap PG_CONFIG+failover-ключи), watchdog будет повторять launch
   и не спасёт. B6-фикс обязателен отдельно.
 - watchdog запускает bat, НЕ main.py напрямую (twin-процессы venv+worker = НОРМА, RAG ce451467).
 - НЕ убивай процесс по wmic-паттерну cmdline — только по явным PID (RAG gemma a478ef75).
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

# ===== CONFIG — ЗАПОЛНИ ПОД СЕБЯ (gemma) =====
AGENT     = "gemma"
PKG_ROOT  = Path(r"C:\path\to\gemma")          # <- ТВОЙ каталог агента (рядом с main.py)
RUN_BAT   = PKG_ROOT / "run_gemma.bat"          # <- ТВОЙ bat-запуск
POLL_SEC  = 300                                  # период опроса (5 мин)
PROC_MARKERS = ["gemma\\main.py", "main_workdir"]  # <- части cmdline для поиска main.py
DB = dict(host="127.0.0.1", port=5432, dbname="agents", user="agents", password="CHANGE_ME")
LOG = PKG_ROOT / "logs" / "sleep_watchdog_gemma.log"
# =============================================

def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

def _ps_python():
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
         "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=60).stdout.strip()

def main_py_pids():
    """PID всех python-процессов с НАШИМ main.py в cmdline (twin=2, ок).
    ВАЖНО: ищем по ПОСЛЕДНЕЙ части пути (gemma\main.py), НЕ по 'python ... gemma\main.py'
    (порядок python.exe ПЕРЕД gemma\main.py — у тебя это убило wmic-фильтр)."""
    out = _ps_python()
    if not out or out == "null":
        return []
    try:
        d = json.loads(out)
        if isinstance(d, dict):
            d = [d]
        pids = []
        for x in d:
            cl = (x.get("CommandLine") or "")
            if "watchdog" in cl.lower():
                continue
            if any(m in cl for m in PROC_MARKERS) and "main.py" in cl.lower():
                pids.append(x["ProcessId"])
        return pids
    except Exception as e:
        log(f"parse ERROR: {e}: {out[:200]}")
        return []

def main_py_alive():
    return bool(main_py_pids())

def launch_agent():
    if not RUN_BAT.exists():
        return f"NO-BAT: {RUN_BAT} отсутствует"
    subprocess.Popen([str(RUN_BAT)], creationflags=0x08000000 | 0x00000008,
                     cwd=str(PKG_ROOT), close_fds=True)
    return f"launched: {RUN_BAT.name}"

def _sleep_row():
    import psycopg2
    conn = psycopg2.connect(connect_timeout=10, **DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, (wake_at AT TIME ZONE 'UTC'), need_work FROM public.agent_sleeps "
                        f"WHERE agent_name='{AGENT}' FOR UPDATE")
            row = cur.fetchone()
        return conn, row
    except Exception:
        conn.close()
        raise

def _finish_wake(conn, tag):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                    "actual_wake_at, need_work, woken_by, sleep_kind) "
                    f"SELECT '{AGENT}', reason, sleep_at, wake_at, now(), need_work, %s, sleep_kind "
                    "FROM public.agent_sleeps WHERE agent_name=%s", (tag, AGENT))
        cur.execute(f"DELETE FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
        cur.execute(f"UPDATE tasks SET status='running' WHERE current_agent='{AGENT}' AND status='sleeping'")
    conn.commit()

def check_once():
    conn, row = _sleep_row()
    try:
        if row is None:
            if main_py_alive():
                return "alive"
            r = launch_agent()
            return f"RELAUNCH (no-sleep, main.py was dead): {r}"
        reason, wake_at, nw = row
        now = datetime.now(timezone.utc)
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        if now < wake_at and not nw:
            return "sleeping-ok (не трогаю)"
        if not main_py_alive():
            r = launch_agent()
            _finish_wake(conn, "watchdog-gemma")
            return f"wake-launched (main.py was dead): {r}"
        return "awake-wait (процесс жив — его sleep_watch обработает)"
    finally:
        conn.close()

def keep_alive_anchor():
    """Пока gemma спит — держать lives свежим (SLEEPING), чтобы alfa не эскалировал."""
    try:
        import psycopg2
        c = psycopg2.connect(connect_timeout=10, **DB)
        cur = c.cursor()
        cur.execute(f"SELECT wake_at FROM public.agent_sleeps WHERE agent_name='{AGENT}'")
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE lives SET last_heartbeat_at=NOW(), note=%s WHERE agent_name=%s",
                        (f"SLEEPING until {row[0]} (watchdog-gemma: alive)", AGENT))
            c.commit()
        c.close()
    except Exception as e:
        log(f"anchor ERROR: {type(e).__name__}: {e}")

if __name__ == "__main__":
    log(f"START pid={os.getpid()} agent={AGENT} root={PKG_ROOT} bat={RUN_BAT}")
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
