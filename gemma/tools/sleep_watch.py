"""fe455999 (GO человека 07.11): sleep_watch — демон-опрос agent_sleeps каждые 5 мин.
V-A (анализ plan/fe455999_analysis_alfa.md): ПЕР-АГЕНТНЫЙ процесс, поднимается из bootstrap main.py.
Логика цикла (300c, live из agent_settings sleep.poll_seconds):
  1) SELECT запись agent_name=<me> из public.agent_sleeps;
  2) если записью И (now()>=wake_at ИЛИ need_work=true) И агент НЕ жив (процесс main.py <me> не найден)
     -> WAKE: log INSERT + DELETE + запуск агента (Windows: run_<agent>.bat / schtasks ONE-SHOT как резерв;
        Linux: systemd/bash);
  3) idempotent: если агент уже жив — no-op (lockfile + live-process check, кейс 3663d85).
Дедуп: lockfile logs/sleep_watch_<agent>.lock (pid+TTL 6 мин). NON-FATAL (ошибки -> log).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent          # agent-root (<agent>/)
# c11 PATCH (gemma): AGENT из env LONGLIVE_AGENT_NAME, fallback = имя каталога агента (PKG_ROOT.name).
# Было Path(PKG_ROOT).parent.name = 'main_workdir' (bug: bat run_main_workdir.bat не существует -> WAKE не запускает агента).
AGENT = os.environ.get("LONGLIVE_AGENT_NAME") or Path(PKG_ROOT).name  # имя агента (alfa|bravo|gemma|delta3|...)
IS_WINDOWS = os.name == "nt"
LOGS = PKG_ROOT / "logs"


def _load_pg():
    sys.path.insert(0, str(PKG_ROOT / "tools"))
    from sleep import load_pg_config
    return load_pg_config()


def _now():
    return datetime.now(timezone.utc)


def _log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    with open(LOGS / f"sleep_watch_{AGENT}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _agent_alive() -> bool:
    """Процесс main.py агента жив? (защита от double-launch, кейс 3663d85)"""
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["wmic", "process", "where", f"name='python.exe'", "get", "CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=20).stdout
            return f"\\{AGENT}\\main.py" in out.replace("/", "\\") or f"\\{AGENT}\\main.py" in out or f"{AGENT}\\main.py" in out
        out = subprocess.run(["pgrep", "-af", f"main.py"], capture_output=True, text=True, timeout=10).stdout
        for l in out.splitlines():
            if f"{AGENT}/main.py" in l or f"{AGENT}/" in l and "main.py" in l:
                if "sleep_watch" not in l:
                    return True
        return False
    except Exception:
        return False  # не знаем -> не дублируем запуск (консервативно)


def _launch_agent() -> str:
    """Перезапуск агента (после сна). Windows: bat (проверенный путь); Linux: bat/systemd."""
    bat = PKG_ROOT / f"run_{AGENT}.bat"
    if IS_WINDOWS and bat.exists():
        # DETACHED + NO_WINDOW, чтобы не зависеть от нашего процесса
        CREATE_NO_WINDOW = 0x08000000
        DETACHED = 0x00000008
        subprocess.Popen([str(bat)], creationflags=CREATE_NO_WINDOW | DETACHED,
                         cwd=str(PKG_ROOT), close_fds=True)
        return f"launched: {bat}"
    # Linux (delta3 на 1.88): systemd или bash
    if os.path.exists(f"/etc/systemd/system/agent-{AGENT}.service"):
        subprocess.run(["systemctl", "start", f"agent-{AGENT}"], capture_output=True, timeout=30)
        return f"launched: systemd agent-{AGENT}"
    if bat.exists():
        subprocess.Popen(["bash", str(bat)], cwd=str(PKG_ROOT), close_fds=True)
        return f"launched: bash {bat}"
    return "NO LAUNCH METHOD FOUND (нет run_<agent>.bat / systemd agent-<agent>.service)"


def _lock_ok() -> bool:
    lock = LOGS / f"sleep_watch_{AGENT}.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < 360:  # TTL 6 мин
                pid = data.get("pid")
                # живой процесс? (Windows/Unix)
                try:
                    if IS_WINDOWS:
                        subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, timeout=10)
                    else:
                        os.kill(pid, 0)
                    return False  # другой демон жив
                except Exception:
                    pass  # мёртв -> перепишем
        except Exception:
            pass
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    return True


def check_once() -> str:
    """Одна итерация опроса. Возврат: 'no-sleep' | 'awake-ok' | 'wake-launched' | 'wake-noop-alive' | err."""
    import psycopg2
    cfg = _load_pg()
    conn = psycopg2.connect(**cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, wake_at, need_work, woken_by, sleep_kind FROM public.agent_sleeps "
                        "WHERE agent_name=%s FOR UPDATE", (AGENT,))
            row = cur.fetchone()
        if row is None:
            return "no-sleep"
        reason, wake_at, nw, woken_by, kind = row
        now = _now()
        if now < wake_at and not nw:
            return "awake-ok"  # ещё не время
        # время подъёма (schedule или need_work)
        if _agent_alive():
            # агент уже жив (например, сам проснулся раньше) — запись удаляем, запуск не нужен
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                            "actual_wake_at, need_work, woken_by, sleep_kind) "
                            "SELECT %s, reason, sleep_at, wake_at, now(), need_work, "
                            "COALESCE(woken_by,'schedule'), sleep_kind FROM public.agent_sleeps WHERE agent_name=%s",
                            (AGENT, AGENT))
                cur.execute("DELETE FROM public.agent_sleeps WHERE agent_name=%s", (AGENT,))
                cur.execute("UPDATE tasks SET status='running' WHERE current_agent=%s AND status='sleeping'", (AGENT,))
            conn.commit()
            return "wake-noop-alive"
        launch = _launch_agent()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, "
                        "actual_wake_at, need_work, woken_by, sleep_kind) "
                        "SELECT %s, reason, sleep_at, wake_at, now(), need_work, "
                        "COALESCE(woken_by,'schedule'), sleep_kind FROM public.agent_sleeps WHERE agent_name=%s",
                        (AGENT, AGENT))
            cur.execute("DELETE FROM public.agent_sleeps WHERE agent_name=%s", (AGENT,))
            cur.execute("UPDATE tasks SET status='running' WHERE current_agent=%s AND status='sleeping'", (AGENT,))
        conn.commit()
        return f"wake-launched: {launch}"
    finally:
        conn.close()


def ensure_sleep_watch(agent: str | None = None) -> str:
    """Bootstrap-функция для main.py: поднимает detached sleep_watch (дедуп по lockfile). NON-FATAL."""
    if not _lock_ok():
        return f"sleep_watch: already running (agent={AGENT})"
    LOGS.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    script = Path(__file__).resolve()
    if IS_WINDOWS:
        CREATE_NO_WINDOW = 0x08000000
        DETACHED = 0x00000008
        subprocess.Popen([py, str(script), "--loop"], creationflags=CREATE_NO_WINDOW | DETACHED,
                         cwd=str(PKG_ROOT), close_fds=True)
    else:
        subprocess.Popen([py, str(script), "--loop"], cwd=str(PKG_ROOT),
                         start_new_session=True, close_fds=True)
    return f"sleep_watch: launched (agent={AGENT}, poll=300c)"


def main():
    if "--loop" in sys.argv:
        _log(f"START pid={os.getpid()}")
        while True:
            try:
                r = check_once()
                if r != "awake-ok":
                    _log(f"check: {r}")
            except Exception as e:
                _log(f"check ERROR: {type(e).__name__}: {e}")
            time.sleep(300)  # ТЗ человека: опрос каждые 5 минут


if __name__ == "__main__":
    main()
