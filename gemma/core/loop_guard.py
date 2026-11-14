"""Loop guard (задача 7881bfef, owner alfa, 2026-09-23): автоматическая защита от bootstrap-лупа.

Bootstrap-луп (доказан в БД, analyst_daemon_queue 21:01:08-21:01:44): цикл падает с ошибкой
(erase слота / 500 tool-call / connection), main.py глотает исключение, пишет heartbeat (lives)
и через restart_delay_seconds запускает тот же цикл заново. Агент "жив" по якорю, но не делает
работы; напарник, глядя только на lives, проблему НЕ видит.

Защита (все функции НЕ роняют агента: ошибки -> logging.warning):
- L1 СЧЁТЧИК: consecutive no-progress циклов; «прогресс» = tool-call в цикле ИЛИ normal-причина
  завершения; состояние в logs/loop_state.json (переживает рестарт ПРОЦЕССА).
- L2 BACKOFF: при застревании sleep экспоненциально base->cap (30->900s), сброс на прогресс.
- L3 ФИКСАЦИЯ: agent_operations_log (stage='loop_guard'), lives.note='STUCK xN ...'
  (видна напарнику одним SELECT по якорю), WARNING в alfa.txt.
- L4 ЭСКАЛАЦИЯ: human_request status='wait' с подсказкой по типу причины; дедупликация по
  reason_key с cooldown loop_escalate_cooldown_seconds.
- L5 SELF-HEAL: erase_with_retry (ожидание+повтор) и компактная пересборка bootstrap-промпта
  (build_longlive_restart_prompt(compact=True) в longlive_memory.py) при oversized-памяти.
- L6 CRASH-WATCH: crash_watch() при старте процесса — если последний чекпоинт предыдущего
  процесса имеет error-причину, фиксирует в agent_operations_log (stage='crash_watch',
  action='prev_process_died'): crash виден в БД, даже если напарник мёртв.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from gemma.core.config import AGENT_ROOT, PG_CONFIG, get_runtime_settings

# Причины НОРМАЛЬНОГО завершения цикла (не ошибка -> сброс счётчика).
NORMAL_PREFIXES = (
    'context reserve reached',
    'no tool calls in',
    'continuing in the same context',
    'generation complete',
)

# Подсказки для эскалации человеку (L4).
REASON_HINTS = {
    'slot_not_erased': 'слот llama-server не стирается: проверить /slots (слот занят зависшим процессом), перезапустить llama-server.',
    'tool_json_500': 'модель режет JSON tool-call (500 parse error): уменьшить completion_max_tokens или сменить модель.',
    'bootstrap_oversized': 'bootstrap-промпт не помещается в контекст: память раздулась (recent_steps/key_facts), почистить analyst_daemon_queue.',
    'connection_error': 'llama-server недоступен: проверить процесс и порт 8080.',
    'db_error': 'PostgreSQL недоступен/медленный: проверить 127.0.0.1:5432.',
}


def classify_reason(reason):
    """(kind, key): kind='normal'|'error', key — устойчивая категория причины."""
    r = (reason or '').strip()
    if not r or r.startswith(NORMAL_PREFIXES):
        return 'normal', 'normal'
    low = r.lower()
    if 'slot was not erased' in low:
        return 'error', 'slot_not_erased'
    if 'failed to parse tool call' in low or 'tool call arguments' in low:
        return 'error', 'tool_json_500'
    if 'exceeds context budget' in low:
        return 'error', 'bootstrap_oversized'
    if 'psycopg2' in low or 'connection to server' in low:
        return 'error', 'db_error'
    if 'connection' in low:
        return 'error', 'connection_error'
    return 'error', 'other_error'


class LoopGuard:
    """Счётчик no-progress циклов + backoff + фиксация + эскалация. См. модуль-документацию."""

    def __init__(self, agent_name, task_id, state_path=None):
        self.agent_name = agent_name
        self.task_id = task_id
        self.state_path = state_path or (AGENT_ROOT / 'logs' / 'loop_state.json')
        self.state = self._load_state()

    def _load_state(self):
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    data.setdefault('consecutive_no_progress', 0)
                    data.setdefault('backoff_s', 0)
                    data.setdefault('last_escalated', {})
                    return data
        except Exception as exc:
            logging.warning('loop_guard: state read failed (%s): %s', type(exc).__name__, exc)
        return {'consecutive_no_progress': 0, 'backoff_s': 0, 'last_escalated': {},
                'last_error_reason': '', 'last_error_key': ''}

    def _save_state(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        except Exception as exc:
            logging.warning('loop_guard: state save failed: %s', exc)

    def on_cycle_finished(self, reason, had_tools):
        """Вызывать в finally каждого цикла. Возвращает sleep перед следующим циклом (сек)."""
        settings = get_runtime_settings()
        threshold = settings.get('loop_stuck_threshold', 3)
        base = settings.get('loop_backoff_base_seconds', 30)
        cap = settings.get('loop_backoff_cap_seconds', 900)
        default_delay = settings.get('restart_delay_seconds', 5)
        try:
            kind, key = classify_reason(reason)
            if had_tools or kind == 'normal':
                if self.state['consecutive_no_progress'] > 0:
                    self._log_op('loop_guard_stuck_reset', {'reason': reason[:300]})
                    self._lives_note(f"reset after STUCK x{self.state['consecutive_no_progress']}")
                self.state.update({'consecutive_no_progress': 0, 'backoff_s': 0,
                                   'last_error_reason': '', 'last_error_key': ''})
                self._save_state()
                return default_delay
            # no progress
            self.state['consecutive_no_progress'] += 1
            self.state['last_error_reason'] = (reason or '')[:500]
            self.state['last_error_key'] = key
            n = self.state['consecutive_no_progress']
            if n < threshold:
                logging.warning('loop_guard: no-progress cycle %s/%s (%s)', n, threshold, key)
                self._save_state()
                return default_delay
            backoff = min(cap, base * (2 ** (n - threshold)))
            self.state['backoff_s'] = backoff
            self._log_op('loop_guard_stuck_detected', {'consecutive': n, 'reason_key': key,
                                            'reason': (reason or '')[:500], 'backoff_s': backoff,
                                            'model': self.state.get('last_model', ''),
                                            'slot': self.state.get('last_slot', '')})
            self._lives_note(f'STUCK x{n}: {key} (backoff {backoff}s)')
            self._maybe_escalate(key, reason, n, backoff, settings)
            self._save_state()
            return backoff
        except Exception as exc:
            logging.warning('loop_guard: on_cycle_finished failed (%s): %s', type(exc).__name__, exc)
            return default_delay

    def _maybe_escalate(self, key, reason, n, backoff, settings):
        cooldown = settings.get('loop_escalate_cooldown_seconds', 1800)
        now = datetime.now(timezone.utc)
        last = self.state.get('last_escalated', {}).get(key)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < cooldown:
                    return
            except ValueError:
                pass
        hint = REASON_HINTS.get(key, 'проверить лог агента и llama-server.')
        quest = ('АЛФА ЗАСТРЕЛА (loop-guard, 7881bfef): %d циклов подряд без прогресса. '
                 'Причина: %s\nBackoff запущен (%ds, max %ds); агент будет повторять. '
                 'Эскалация по этой причине повторится не ранее чем через %d мин.\nПодсказка: %s'
                 % (n, (reason or '')[:300], backoff, settings.get('loop_backoff_cap_seconds', 900),
                    int(cooldown / 60), hint))
        try:
            import psycopg2
            conn = psycopg2.connect(**PG_CONFIG)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO human_request (task_id, status, human_approved, agent_task, agent_quest, created_at) "
                    "VALUES (%s, 'wait', FALSE, %s, %s, NOW()) RETURNING request_id",
                    (self.task_id, self.agent_name, quest))
                rid = cur.fetchone()[0]
                conn.commit()
            finally:
                conn.close()
            self.state.setdefault('last_escalated', {})[key] = now.isoformat()
            self._log_op('loop_guard_escalated', {'request_id': str(rid), 'reason_key': key, 'consecutive': n})
        except Exception as exc:
            logging.warning('loop_guard: escalation FAILED (%s): %s', type(exc).__name__, exc)

    def _log_op(self, action, details):
        try:
            import psycopg2
            conn = psycopg2.connect(**PG_CONFIG)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO agent_operations_log (task_id, stage, action, details, created_at) "
                    "VALUES (%s, 'error', %s, %s::jsonb, NOW())  -- CHECK stage: analyze/plan/approval/execute/review/error",
                    (self.task_id, action, json.dumps(details, ensure_ascii=False, default=str)))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logging.warning('loop_guard: operations log FAILED (%s): %s', type(exc).__name__, exc)

    def _lives_note(self, extra):
        try:
            import psycopg2
            conn = psycopg2.connect(**PG_CONFIG)
            try:
                cur = conn.cursor()
                # 2a23c2d2 S3 (mirror bravo S2): STUCK-агент (жив, в backoff) = жив по
                # timestamp — добавляем last_heartbeat_at (раньше только note).
                cur.execute('UPDATE lives SET note=%s, last_heartbeat_at=NOW(), updated_at=NOW() WHERE agent_name=%s',
                            (str(extra)[:400], self.agent_name))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logging.warning('loop_guard: lives note FAILED (%s): %s', type(exc).__name__, exc)


def erase_with_retry(settings=None, retries=1, wait_seconds=10):
    """L5: erase слота с одним повтором после ожидания (слот мог освободиться сам)."""
    from gemma.core.llama_runtime import erase_llama_slot
    if erase_llama_slot(settings=settings):
        return True
    for attempt in range(1, retries + 1):
        logging.warning('loop_guard: slot erase failed, retry %s/%s in %ss', attempt, retries, wait_seconds)
        import time
        time.sleep(wait_seconds)
        if erase_llama_slot(settings=settings):
            return True
    return False


def crash_watch(agent_name, task_id):
    """L6: при старте процесса — если последний чекпоинт предыдущего процесса имеет
    error-причину, записать в agent_operations_log (crash виден в БД, даже без напарника)."""
    try:
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT q.agent_reasoning, q.created_at FROM analyst_daemon_queue q
                   WHERE q.request_type='checkpoint' AND q.request_payload->>'agent'=%s
                   ORDER BY q.created_at DESC LIMIT 1""", (agent_name,))
            row = cur.fetchone()
            if not row or not row[0]:
                return
            kind, key = classify_reason(str(row[0]))
            if kind == 'normal':
                return
            cur.execute(
                "INSERT INTO agent_operations_log (task_id, stage, action, details, created_at) "
                "VALUES (%s, 'error', 'crash_watch_prev_process_died', %s::jsonb, NOW())",
                (task_id, json.dumps({'prev_reason': str(row[0])[:500], 'reason_key': key,
                                      'prev_checkpoint_at': str(row[1])}, ensure_ascii=False)))
            conn.commit()
            logging.warning('loop_guard: previous process DIED with error: %s', key)
        finally:
            conn.close()
    except Exception as exc:
        logging.warning('loop_guard: crash_watch FAILED (%s): %s', type(exc).__name__, exc)
