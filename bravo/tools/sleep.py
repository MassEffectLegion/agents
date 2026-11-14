"""fe455999 (GO человека HR 099f9b40 07.11): инструмент спячки — У ВСЕХ основных агентов.
ТЗ: запись в agent_sleeps (reason, sleep_at, wake_at); при просыпании запись удаляется;
флаг need_work + опрос каждые 5 мин (sleep_watch) -> автоматический запуск агента.
Портативный: PG из env AGENT_SETTINGS_FILE/runtime_settings.local.json (как ssh_creds_resolver).
"""
import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent  # agent-root


def load_pg_config():
    import json
    import os
    cfg = {}
    env_cfg = os.environ.get("AGENT_SETTINGS_FILE")
    candidates = [Path(env_cfg)] if env_cfg else []
    candidates += [PKG_ROOT / "runtime_settings.local.json",
                   Path(r"<AGENTS_DIR>\main_workdir\bravo\runtime_settings.local.json"),
                   Path(r"<AGENTS_DIR>\main_workdir\alfa\runtime_settings.local.json")]
    for p in candidates:
        try:
            if p.exists():
                cfg = dict(json.loads(p.read_text(encoding="utf-8")).get("postgres", {}))
                if cfg.get("host"):
                    break
        except Exception:
            continue
    for key, env in {"host": "PGHOST", "port": "PGPORT", "database": "PGDATABASE",
                     "user": "PGUSER", "password": "PGPASSWORD"}.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not cfg.get("host"):
        raise RuntimeError("PG host не найден (AGENT_SETTINGS_FILE/PGHOST)")
    # FIX B5 (rest#17, по gemma c11 id=55): whitelist psycopg2-ключей.
    # failover-контейнеры (gemma c9) имеют в postgres-секции databases[]/failover ->
    # "invalid connection option databases". Без whitelist деплой ломается у gemma.
    ALLOWED = ("host", "port", "database", "user", "password", "options",
               "connect_timeout", "dbname", "sslmode")
    return {k: v for k, v in cfg.items() if k in ALLOWED}


def _conn():
    import psycopg2
    cfg = load_pg_config()
    cfg.setdefault("connect_timeout", 10)
    return psycopg2.connect(**cfg)


def _now():
    return datetime.now(timezone.utc)


def _agent_name():
    """FIX bd3ebd57 (live-тест rest#16): было .parent.parent.parent.name = 'main_workdir' (BUG);
    PKG_ROOT = <agent-root> (parent.parent), имя = PKG_ROOT.name. ENV override сохраняется."""
    import os
    n = os.environ.get("LONGLIVE_AGENT_NAME") or os.environ.get("AGENT_NAME")
    if n:
        return n
    return PKG_ROOT.name  # .../tools/sleep.py -> PKG_ROOT=<agent>


def parse_duration(s: str):
    """'45m' | '2h' | '2.5h' | 'HH:MM' (время суток) | '2026-11-08T09:00' (ts). -> (wake_at, label)"""
    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m|min|h|d)", s)
    if m:
        v, unit = float(m.group(1)), m.group(2)
        delta = timedelta(minutes=v if unit in ("m", "min") else v * 60 if unit == "h" else v * 24 * 60)
        return _now() + delta, s
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        now = _now()
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t, s + " (завтра, если время прошло)"
    try:
        return datetime.fromisoformat(s), s
    except ValueError:
        raise RuntimeError(f"нечитаемый период: {s!r} (форматы: 45m, 2h, 09:00, ISO-timestamptz)")


def pre_checks(agent: str) -> str | None:
    """REFUSE-условия (v1-наследие + ТЗ): свежий чекпоинт, нет влиятельных непрочитанных.
    Возвращает причину отказа или None."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(created_at) FROM analyst_daemon_queue "
                        "WHERE request_payload->>'agent'=%s AND request_type='checkpoint'", (agent,))
            last_cp = cur.fetchone()[0]
            if last_cp is None or (_now() - last_cp) > timedelta(minutes=30):
                return ("REFUSE: нет свежего чекпоинта (analyst_daemon_queue <30 мин) — "
                        "сначала save_progress, потом sleep.")
            cur.execute("SELECT COUNT(*) FROM tg_message_queue WHERE status='new'")
            tg = cur.fetchone()[0]
            if tg:
                return f"REFUSE: есть {tg} непрочитанных TG-сообщений (tg_message_queue status=new)."
        return None
    finally:
        conn.close()


def sleep_start(agent: str, period: str, reason: str, kind: str = "planned") -> str:
    wake_at, label = parse_duration(period)
    refuse = pre_checks(agent)
    if refuse:
        return refuse
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.agent_sleeps WHERE agent_name=%s", (agent,))  # idempotent
            cur.execute("INSERT INTO public.agent_sleeps (agent_name, reason, wake_at, sleep_kind) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (agent_name) DO UPDATE SET "
                        "reason=EXCLUDED.reason, wake_at=EXCLUDED.wake_at, sleep_kind=EXCLUDED.sleep_kind, "
                        "need_work=false, updated_at=now()", (agent, reason, wake_at, kind))
            note = f"SLEEPING until {wake_at.isoformat()} ({reason}, {kind})"
            cur.execute("UPDATE lives SET note=%s WHERE agent_name=%s", (note, agent))
            cur.execute("UPDATE tasks SET status='sleeping' WHERE current_agent=%s AND status='running'", (agent,))
        conn.commit()
    finally:
        conn.close()
    return (f"✅ SLEEP: {agent} засыпает {label} (wake_at={wake_at.isoformat()}, reason={reason}). "
            f"Ранний подъём: UPDATE public.agent_sleeps SET need_work=true WHERE agent_name='{agent}' "
            f"(или sleep --need_work {agent}). Агент завершит текущий цикл и остановится; "
            f"sleep_watch (5 мин) перезапустит его в wake_at/need_work.")


def need_work(agent: str, by: str = "agent") -> str:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.agent_sleeps SET need_work=true, woken_by=%s, "
                        "updated_at=now() WHERE agent_name=%s", (f"need_work:{by}", agent))
            n = cur.rowcount
        conn.commit()
        return f"✅ NEED_WORK: {agent} (поставил {by})" if n else f"⚠️ {agent} не спит (нет записи agent_sleeps)"
    finally:
        conn.close()


def wake_cancel(agent: str, by: str = "agent", mode: str = "cancel") -> str:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reason, sleep_at, wake_at, need_work, sleep_kind FROM public.agent_sleeps WHERE agent_name=%s", (agent,))
            row = cur.fetchone()
            if row is None:
                return f"⚠️ {agent} не спит (нет записи agent_sleeps)"
            reason, sleep_at, wake_at, nw, kind = row
            cur.execute("DELETE FROM public.agent_sleeps WHERE agent_name=%s", (agent,))
            cur.execute("INSERT INTO public.agent_sleeps_log (agent_name, reason, sleep_at, wake_at, actual_wake_at, "
                        "need_work, woken_by, sleep_kind) VALUES (%s,%s,%s,%s,now(),%s,%s,%s)",
                        (agent, reason, sleep_at, wake_at, nw, f"{mode}:{by}", kind))
            cur.execute("UPDATE tasks SET status='running' WHERE current_agent=%s AND status='sleeping'", (agent,))
            # FIX bd3ebd57 rest#16: сбрасываем stale note='SLEEPING ...' (иначе watchdog-логика
            # видит агент "спит" до начала следующего цикла; report_alive перезапишет, но не раньше).
            # FIX rest#27b: ВНУТРЬ try (до commit) — было ПОСЛЕ finally:conn.close() ->
            # InterfaceError: cursor already closed (cancel падал после DELETE+log,
            # но lives.note/tasks.status не обновлялись до commit).
            cur.execute("UPDATE lives SET note=%s WHERE agent_name=%s",
                        (f"WAKE-{mode.upper()} ({by}) — note сброслен sleep.py", agent))
        conn.commit()
        return f"✅ {mode.upper()}: {agent} (запись сна удалена, лог записан, lives.note сброшен)"
    finally:
        conn.close()


def status(agent: str | None = None) -> str:
    agents = [agent] if agent else ["alfa", "bravo", "gemma", "delta3"]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_name, reason, sleep_at, wake_at, need_work, woken_by, sleep_kind "
                        "FROM public.agent_sleeps WHERE agent_name=ANY(%s)", (agents,))
            rows = cur.fetchall()
            out = ["Спящие (agent_sleeps):"]
            if not rows:
                out.append("  — никто не спит")
            for r in rows:
                out.append(f"  • {r[0]}: reason={r[1]} sleep_at={r[2]} wake_at={r[3]} need_work={r[4]} kind={r[6]}")
            cur.execute("SELECT agent_name, reason, sleep_at, wake_at, actual_wake_at, woken_by FROM public.agent_sleeps_log "
                        "WHERE agent_name=ANY(%s) ORDER BY created_at DESC LIMIT 10", (agents,))
            logs = cur.fetchall()
            if logs:
                out.append("Журнал (последние 10):")
                for r in logs:
                    out.append(f"  • {r[0]}: {r[1]} {r[2]}->{r[4]} ({r[5]})")
        return "\n".join(out)
    finally:
        conn.close()


def _cli(argv=None):
    p = argparse.ArgumentParser(description="Инструмент спячки (fe455999)")
    p.add_argument("--agent", default=None, help="агент (по умолчанию свой)")
    p.add_argument("--sleep", dest="period", metavar="PERIOD", help="45m | 2h | 09:00 | ISO-ts")
    p.add_argument("--reason", default="no_tasks")
    p.add_argument("--kind", default="planned", choices=["planned", "emergency", "llm_test"])
    p.add_argument("--need_work", dest="nw", metavar="AGENT", help="поставить флаг раннего подъёма")
    p.add_argument("--wake", metavar="AGENT", help="проснулось/ручной подъём (удалить запись)")
    p.add_argument("--cancel", metavar="AGENT", help="отменить сон (удалить запись + лог)")
    p.add_argument("--status", dest="st", nargs="?", const="all")
    a = p.parse_args(argv)
    ag = a.agent or _agent_name()
    if a.period:
        return sleep_start(ag, a.period, a.reason, a.kind)
    if a.nw:
        return need_work(a.nw or ag)
    if a.wake:
        return wake_cancel(a.wake, mode="manual")
    if a.cancel:
        return wake_cancel(a.cancel, mode="cancel")
    if a.st or True:
        return status(ag if a.agent else None)


def _cli_lenient(tokens):
    """P1/15f76c90: lenient-обёртка над _cli.
    Корень D1: shlex.split('... --reason no_tasks: все pending') -> --reason берёт ТОЛЬКО 'no_tasks:',
    остальное = unrecognized arguments -> argparse SystemExit(2) -> агент УМИРАЕТ.
    Фикс: (1) жадно склеиваем хвост за --reason в ОДИН аргумент; (2) ловим SystemExit -> читаемая строка
    (НЕ исключение). main.py P2 дополнительно страхует BaseException.
    """
    known_flags = {"--agent", "--sleep", "--reason", "--kind", "--need_work", "--wake", "--cancel", "--status"}
    out, i, n = [], 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t == "--reason" and i + 1 < n:
            j = i + 1; buf = []
            while j < n and not (tokens[j] in known_flags):
                buf.append(tokens[j]); j += 1
            out.append(t); out.append(" ".join(buf)); i = j
        else:
            out.append(t); i += 1
    try:
        return _cli(out)
    except SystemExit as e:
        return (f"❌ SLEEP-CLI ОШИБКА (lenient, P1/15f76c90): argparse не принял аргументы (code={e.code}). "
                f"Присланный command: {tokens!r}. Формат: --sleep 45m --reason snake_case. "
                f"Агент НЕ остановлен — ошибка возвращена как текст.")
    except Exception as e:
        return f"❌ SLEEP-CLI ОШИБКА: {type(e).__name__}: {e}"

def sleep(command: str = "--status") -> str:
    """LLM-инструмент: спячка агента (fe455999, GO человека 07.11).
    Формат command: '--sleep 45m --reason no_tasks' | '--status' | '--need_work alfa' | '--wake alfa' | '--cancel alfa'.
    ВАЖНО: вызывать ТОЛЬКО в спокойном состоянии (свежий чекпоинт <30 мин — иначе REFUSE).
    После успешного --sleep агент ДОЛЖЕН немедленно завершить цикл (request_restart / завершение),
    main.py сам остановит его (sleep-guard).
    P1/15f76c90: reason с пробелами больше НЕ убивает агента (lenient + SystemExit-catch)."""
    import shlex
    return _cli_lenient(shlex.split(command))


if __name__ == "__main__":
    # FIX B7 (rest#18, 08.11): без блока __main__ CLI-запуск (python sleep.py --sleep ...)
    # молча ничего НЕ делал (return без print) — запись сна не создавалась (инцидент rest#17).
    import sys
    print(_cli(sys.argv[1:]))
