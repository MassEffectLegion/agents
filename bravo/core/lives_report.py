"""lives-отчёт (заказ человека 2026-09-23, ответ на 9b05b408; доработка bravo, 2026-09-23).

Записывает информацию об агенте в таблицу lives:
- report_start(): при старте процесса — upsert +restart_count + время старта.
- report_alive(): при каждом цикле / крупном этапе — обновление last_heartbeat_at + заметка.

Совместимо с alfa2-версией (тот же API: checkpoint-объект опционален).
Если checkpoint=None — подключение создаётся самостоятельно (bravo.core.config.PG_CONFIG),
чтобы инструмент save_progress мог вызывать report_alive без checkpoint-объекта.

Логика якоря живости (человек): если last_heartbeat_at обновляется (<1ч) — агент жив;
если нет (>1ч) — агент мёртв -> эскалация человеку + взять задачи погибшего (см. bootstrap_prompt).
Ошибки НЕ роняют агента: logging.warning.
"""
import logging
from datetime import datetime, timezone


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
        from bravo.core.config import PG_CONFIG
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


# === 2a23c2d2 S2 (bravo, 05.10): watchdog-поток + stopping ===
# Якорь живости (человек): lives.last_heartbeat_at свеж (<1ч) => агент жив.
# До фикса обновлялось ТОЛЬКО в начале цикла (report_alive) + save_progress ->
# длинный LLM-цикл/backoff/deadlock => STALE при живом процессе => ложный "мёртв".
# Watchdog: независимый daemon-поток, свой conn, bump_restart=False, note НЕ трогаем
# (note = статус, timestamp = свежесть — оси разделены).
import threading as _threading
import time as _time

_WATCHDOG_LOCK = _threading.Lock()
_WATCHDOG_THREAD = None
_WATCHDOG_STARTED = False


def report_watchdog(agent_name: str) -> bool:
    """Свежить last_heartbeat_at БЕЗ изменения note и restart_count. Свой conn."""
    try:
        from bravo.core.config import PG_CONFIG
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE lives SET last_heartbeat_at=NOW(), updated_at=NOW() WHERE agent_name=%s",
                (agent_name,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — не роняем; поток продолжит
        logging.warning('lives WATCHDOG update FAILED (agent=%s): %s: %s',
                        agent_name, type(exc).__name__, exc)
        return False


def _watchdog_loop(agent_name: str, interval_seconds: int):
    while True:
        try:
            report_watchdog(agent_name)
        except Exception as exc:  # noqa: BLE001
            logging.warning('lives watchdog loop unexpected: %s: %s', type(exc).__name__, exc)
        try:
            _time.sleep(interval_seconds)
        except Exception:  # noqa: BLE001 — прерывание сна при остановке
            return


def start_lives_watchdog(agent_name: str, interval_seconds: int = 120):
    """Запустить daemon-поток watchdog. Идемпотентно (1 поток на процесс)."""
    global _WATCHDOG_THREAD, _WATCHDOG_STARTED
    with _WATCHDOG_LOCK:
        if _WATCHDOG_STARTED:
            return _WATCHDOG_THREAD
        try:
            interval_seconds = int(interval_seconds)
        except Exception:  # noqa: BLE001
            interval_seconds = 120
        if interval_seconds < 10:
            interval_seconds = 120
        _WATCHDOG_THREAD = _threading.Thread(
            target=_watchdog_loop, args=(agent_name, interval_seconds),
            name='lives-watchdog', daemon=True)
        _WATCHDOG_THREAD.start()
        _WATCHDOG_STARTED = True
        logging.info('lives watchdog STARTED (agent=%s interval=%ss)', agent_name, interval_seconds)
        return _WATCHDOG_THREAD


def report_stopping(agent_name: str, reason: str = 'atexit') -> bool:
    """Graceful stop: note='STOPPING (reason)' + ts=NOW(). (kill -9 не перехватить.)"""
    note = ('STOPPING (%s)' % reason)[:400]
    try:
        from bravo.core.config import PG_CONFIG
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE lives SET note=%s, last_heartbeat_at=NOW(), updated_at=NOW() WHERE agent_name=%s",
                (note, agent_name))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logging.warning('lives STOPPING write FAILED (agent=%s): %s: %s', agent_name, type(exc).__name__, exc)
        return False
