"""lives-отчёт (заказ человека 2026-09-23; перенос из alfa2 в delta2/, задача dfa3f65c S2, 2026-09-23).

Записывает информацию об агенте в таблицу lives:
- report_start(): при старте процесса — upsert +restart_count + время старта.
- report_alive(): при каждом цикле — обновление last_heartbeat_at + заметка.

API идентичен версии alfa2/bravo: checkpoint-объект опционален.
Если checkpoint=None — подключение создаётся самостоятельно (delta2.core.config.PG_CONFIG),
чтобы инструменты могли вызывать report_alive без checkpoint-объекта.

Логика якоря живости (человек): если last_heartbeat_at обновляется (<1ч) — агент жив;
если нет (>1ч) — агент мёртв -> эскалация человеку (см. bootstrap_prompt.yaml,
раздел "ЯКОРЬ ЖИВОСТИ НАПАРНИКА").
Ошибки НЕ роняют агента: logging.warning.

2a23c2d2 S3 (05.10, зеркало bravo S2; design lock: plan/2a23c2d2_plan.md):
- report_watchdog(): фоновый heartbeat — ТОЛЬКО last_heartbeat_at/updated_at,
  note НЕ трогает, restart_count НЕ растёт (короткое своё соединение).
- start_lives_watchdog(): daemon-поток раз в lives_heartbeat_seconds (default 120с).
  Решает: длинный LLM-цикл / backoff / deadlock в tool-call -> ts всегда свеж.
- report_stopping(): graceful stop (atexit/SIGTERM) — note='STOPPING (reason)'.
  kill -9 не перехватить -> ts stale -> якорь корректно «мёртв» через 1ч (желаемое).
Оси разделены: timestamp = свежесть (watchdog/report_alive), note = статус (cycle/stuck/stopping).
"""
import logging
import threading
import time
from datetime import datetime, timezone

_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG_STARTED = False
_WATCHDOG_THREAD = None


def _note_text(prefix: str, cycle: int, extra: str = '') -> str:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    base = f'{prefix} cycle={cycle} {ts}'
    if extra:
        base = f'{base} | {extra}'
    # note — text; ограничим длину, чтобы не раздувать строку.
    return base[:400]


def _conn(checkpoint):
    """Контекст соединения: пул checkpoint (если есть) или своё."""
    import contextlib
    if checkpoint is not None and hasattr(checkpoint, '_get_conn'):
        return checkpoint._get_conn()

    @contextlib.contextmanager
    def _own():
        from delta2.core.config import PG_CONFIG
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            yield conn
        finally:
            conn.close()
    return _own()


def _upsert(checkpoint, agent_name: str, extra_note: str, bump_restart: bool) -> bool:
    sql = """
        INSERT INTO lives (agent_name, last_heartbeat_at, restart_count, note, updated_at)
        VALUES (%s, NOW(), %s, %s, NOW())
        ON CONFLICT (agent_name) DO UPDATE SET
            last_heartbeat_at = NOW(),
            restart_count = CASE WHEN %s THEN COALESCE(lives.restart_count, 0) + 1 ELSE COALESCE(lives.restart_count, 0) END,
            note = EXCLUDED.note,
            updated_at = NOW()
    """
    with _conn(checkpoint) as conn:
        cur = conn.cursor()
        cur.execute(sql, (agent_name, 1 if bump_restart else 0, extra_note, bump_restart))
        conn.commit()
    return True


def report_start(checkpoint, agent_name: str, task_id: str, extra: str = '') -> bool:
    """При старте процесса: upsert строки агента, +1 restart_count, время старта."""
    note = _note_text('START (process launch)', 0, extra)
    try:
        _upsert(checkpoint, agent_name, note, bump_restart=True)
        logging.info('lives: START reported for %s (task=%s)', agent_name, task_id)
        return True
    except Exception as exc:  # noqa: BLE001 — не роняем агента
        logging.warning('lives START FAILED (agent=%s): %s: %s', agent_name, type(exc).__name__, exc)
        return False


def report_alive(checkpoint, agent_name: str, cycle: int = 0, extra: str = '') -> bool:
    """Каждый цикл: обновить last_heartbeat_at + заметку (без изменения restart_count)."""
    note = _note_text('alive', cycle, extra)
    try:
        _upsert(checkpoint, agent_name, note, bump_restart=False)
        return True
    except Exception as exc:  # noqa: BLE001
        logging.warning('lives ALIVE FAILED (agent=%s cycle=%s): %s: %s', agent_name, cycle, type(exc).__name__, exc)
        return False


def report_watchdog(checkpoint, agent_name: str) -> bool:
    """2a23c2d2 S3: watchdog-heartbeat — ТОЛЬКО last_heartbeat_at + updated_at.

    note НЕ меняет (note хранит последний осмысленный статус), restart_count НЕ растёт.
    Короткое своё соединение на вызов (нет общего conn с LLM-циклом).
    """
    try:
        with _conn(None) as conn:
            cur = conn.cursor()
            cur.execute('UPDATE lives SET last_heartbeat_at=NOW(), updated_at=NOW() WHERE agent_name=%s',
                        (agent_name,))
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — не роняем агента
        logging.warning('lives WATCHDOG FAILED (agent=%s): %s: %s', agent_name, type(exc).__name__, exc)
        return False


def start_lives_watchdog(checkpoint, agent_name: str, interval_seconds: int = 120) -> threading.Thread:
    """2a23c2d2 S3: daemon-поток — раз в interval_seconds вызывает report_watchdog.

    Живой процесс ВСЕГДА свеж (< interval), независимо от длительности LLM-цикла.
    checkpoint намеренно НЕ передаётся в поток (своё короткое conn на итерацию).
    Идемпотентность: 2-й вызов = no-op (возвращает первый поток).
    Падение итерации (DB error) логируется, поток продолжает.
    """
    global _WATCHDOG_STARTED, _WATCHDOG_THREAD
    with _WATCHDOG_LOCK:
        if _WATCHDOG_STARTED:
            return _WATCHDOG_THREAD
        interval = max(10, int(interval_seconds or 120))

        def _loop():
            while True:
                report_watchdog(None, agent_name)
                time.sleep(interval)

        t = threading.Thread(target=_loop, name=f'lives-watchdog-{agent_name}', daemon=True)
        t.start()
        _WATCHDOG_STARTED = True
        _WATCHDOG_THREAD = t
        logging.info('lives watchdog started (agent=%s interval=%ss)', agent_name, interval)
        return t


def report_stopping(checkpoint, agent_name: str, reason: str = 'atexit') -> bool:
    """2a23c2d2 S3: graceful stop — note='STOPPING (reason)' + ts-свежесть."""
    note = f'STOPPING ({reason})'[:400]
    try:
        with _conn(None) as conn:
            cur = conn.cursor()
            cur.execute('UPDATE lives SET note=%s, last_heartbeat_at=NOW(), updated_at=NOW() WHERE agent_name=%s',
                        (note, agent_name))
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logging.warning('lives STOPPING FAILED (agent=%s): %s: %s', agent_name, type(exc).__name__, exc)
        return False
