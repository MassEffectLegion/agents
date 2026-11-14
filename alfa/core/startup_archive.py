"""Автоархивация при старте агента (ГО человека 24.09, human_request 1ed0ea3a).

Алгоритм = сценарий v3.3 (plan/1090f61e_archive_scenario_v3_3_bravo.sql),
перенесён в код: выполняется при СТАРТЕ процесса агента (сначала alfa, потом bravo).
Первый ручной прогон v3.1/v3.2 сделан 24.09 13:51-13:53 (alfa); этот модуль делает
то же ПЕРИОДИЧЕСКИ, чтобы архивные таблицы не зависели от фонового процесса человека.

Шаги (одна транзакция, ROLLBACK при ошибке, idempotent):
  A.  done/replied >24ч -> status='archived' (hr + htq).
      Аномалии wait/in_progress >24ч НЕ архивируются: лог agent_coordination
      + ANOMALY-запрос человеку (idempotent). FIX3 (alfa): строки agent_task='archive_bg'
      исключены из аномалий hr (иначе ANOMALY сам становился бы новой аномалией).
      HTQ: задачи с task_notes->>'backlog'='true' — тихий skip (беклог человека,
      не шумим ANOMALY; см. faa25588).
  B.  ВСЕ status='archived' (в т.ч. ручные) -> archive_human_request /
      archive_human_task_queue; DELETE из live только записей этого прогона
      (guard: archived_at > now()-1h AND archive_reason='age_24h_or_manual').
  C.  Чекпоинты (ПЕРМАНЕНТНАЯ форма v3.3/П1b): request_type='checkpoint' уходит
      в archive_checkpoints, когда И задача (archive_human_task_queue) И отчёт
      (archive_human_request: agent_task=agent AND task_id) уже в архиве.
      После первого прогона live-таблицы пусты, поэтому C читает archive_* —
      формула человека «и задача, и отчёт = archived» перманентна.
Ошибки НЕ роняют агента: ROLLBACK + logging.warning, возвращает status='ERROR'.
CLI: python -m alfa.core.startup_archive [--dry-run] [--agent alfa]
"""
import logging
import os
import sys
from datetime import datetime, timezone

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

AGE_HOURS = 24  # окно архивации (общая поправка человека 81f2e607: 24 часа)


def _open_conn(checkpoint):
    """Контекст соединения: пул checkpoint (если есть) или своё (как в lives_report)."""
    import contextlib
    if checkpoint is not None and hasattr(checkpoint, '_get_conn'):
        return checkpoint._get_conn()

    @contextlib.contextmanager
    def _own():
        from alfa.core.config import PG_CONFIG
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            yield conn
        finally:
            conn.close()
    return _own()


def _count(cur, sql, params=None) -> int:
    cur.execute(sql, params or ())
    return int(cur.fetchone()[0])


def _insert_archive_row(cur, live_tbl: str, arch_tbl: str, alias: str, pk: str, base_cols):
    """INSERT...SELECT live->archive: базовые колонки + ОБЩИЕ доп. колонки (например project/tags
    после DDL 413a6222) + archived_at/archive_reason/archived_by. Доп. колонки обнаруживаются
    динамически (INTERSECT information_schema) — после DDL перенос включается автоматически,
    без правки модуля. Идентификаторы валидируются (a-z_0-9), статус='archived', idempotent по PK."""
    import re as _re
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s "
        "INTERSECT SELECT column_name FROM information_schema.columns WHERE table_name = %s "
        "ORDER BY 1", (live_tbl, arch_tbl))
    base = set(base_cols) | {'archived_at', 'archive_reason', 'archived_by'}
    extra = [c for c in (r[0] for r in cur.fetchall())
             if c not in base and _re.fullmatch(r'[a-z_][a-z0-9_]*', c)]
    cols = list(base_cols) + extra + ['archived_at', 'archive_reason', 'archived_by']
    sel = [f'{alias}.{c}' for c in base_cols] + [f'{alias}.{c}' for c in extra] + \
          ["now()", "'age_24h_or_manual'", "'bg_archive'"]
    cur.execute(
        f"INSERT INTO agents_main.{arch_tbl} ({', '.join(cols)}) "
        f"SELECT {', '.join(sel)} FROM agents_main.{live_tbl} {alias} "
        f"WHERE {alias}.status='archived' AND NOT EXISTS "
        f"(SELECT 1 FROM agents_main.{arch_tbl} a WHERE a.{pk} = {alias}.{pk})")
    if extra:
        logging.info('startup archive: extra columns transferred %s -> %s: %s', live_tbl, arch_tbl, extra)


def run_startup_archive(checkpoint=None, agent_name: str = 'alfa', dry_run: bool = False) -> dict:
    """Один прогон автоархивации. dry_run=True — только подсчёты (без изменений данных;
    строка-лог в agent_coordination пишется в обоих случаях, с пометкой dry_run)."""
    result = {
        'agent': agent_name, 'dry_run': dry_run, 'status': 'OK',
        'hr_to_archived': 0, 'htq_to_archived': 0, 'hr_anomalies': 0, 'htq_anomalies': 0, 'hr_unacked_anomalies': 0,
        'hr_moved_to_archive': 0, 'htq_moved_to_archive': 0, 'cp_moved_to_archive': 0,
        'ts': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'error': None,
    }
    with _open_conn(checkpoint) as conn:
        cur = conn.cursor()
        try:
            iv = f'interval \'{AGE_HOURS} hours\''
            # ---------- ШАГ A: перевод в 'archived' ----------
            a_hr = (f"SELECT count(*) FROM agents_main.human_request WHERE status IN ('done','replied') "
                    f"AND human_response IS NOT NULL "
                    f"AND agent_ack_at IS NOT NULL "
                    f"AND GREATEST(COALESCE(finish_at, created_at), COALESCE(updated_at, created_at)) < now() - {iv}")
            a_htq = (f"SELECT count(*) FROM agents_main.human_task_queue WHERE status='done' "
                     f"AND COALESCE(finished_at, created_at) < now() - {iv}")
            if dry_run:
                result['hr_to_archived'] = _count(cur, a_hr)
                result['htq_to_archived'] = _count(cur, a_htq)
            else:
                cur.execute(f"UPDATE agents_main.human_request SET status='archived', updated_at=now() "
                            f"WHERE status IN ('done','replied') "
                            f"AND human_response IS NOT NULL "
                            f"AND agent_ack_at IS NOT NULL "
                            f"AND GREATEST(COALESCE(finish_at, created_at), COALESCE(updated_at, created_at)) < now() - {iv}")
                result['hr_to_archived'] = cur.rowcount
                cur.execute(f"UPDATE agents_main.human_task_queue SET status='archived' "
                            f"WHERE status='done' AND COALESCE(finished_at, created_at) < now() - {iv}")
                result['htq_to_archived'] = cur.rowcount
            # ---------- A-ANO: аномалии >24ч (НЕ архивируем, эскалация) ----------
            # FIX3 (alfa): agent_task<>'archive_bg' — ANOMALY-строки не порождают новых ANOMALY.
            an_hr = (f"SELECT count(*) FROM agents_main.human_request r WHERE r.status IN ('wait','in_progress') "
                     f"AND r.created_at < now() - {iv} AND r.agent_task <> 'archive_bg' "
                     f"AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c WHERE c.action='hr_stale' "
                     f"AND c.payload->>'request_id' = r.request_id::text)")
            # FIX-backlog (alfa): backlog=true — тихий skip (задача намеренно в беклоге).
            an_htq = (f"SELECT count(*) FROM agents_main.human_task_queue t WHERE t.status IN ('new','in_progress') "
                      f"AND t.created_at < now() - {iv} AND COALESCE(t.task_notes->>'backlog','false') <> 'true' "
                      f"AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c WHERE c.action='htq_stale' "
                      f"AND c.payload->>'task_id' = t.task_id::text)")
            result['hr_anomalies'] = _count(cur, an_hr)
            result['htq_anomalies'] = _count(cur, an_htq)
            if not dry_run:
                cur.execute(f"""
                    INSERT INTO agents_main.agent_coordination (agent, topic, action, payload)
                    SELECT 'archive_bg', 'archive_anomaly', 'hr_stale',
                           jsonb_build_object('request_id', r.request_id, 'status', r.status,
                              'created_at', r.created_at::text,
                              'age_hours', round(EXTRACT(EPOCH FROM (now() - r.created_at))/3600.0, 1),
                              'note', 'wait/in_progress >24h — НЕ архивируем, эскалация человеку')
                      FROM agents_main.human_request r
                     WHERE r.status IN ('wait','in_progress') AND r.created_at < now() - {iv}
                       AND r.agent_task <> 'archive_bg'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='hr_stale' AND c.payload->>'request_id' = r.request_id::text)""")
                result['hr_anomalies'] = cur.rowcount
                cur.execute(f"""
                    INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status)
                    SELECT r.task_id, 'archive_bg',
                           'ANOMALY:' || r.request_id || ' — запрос в status=\'' || r.status ||
                           '\' с ' || r.created_at::date || ' (>24ч без движения). Проверьте: не прочитан/застрял?',
                           'wait'
                      FROM agents_main.human_request r
                     WHERE r.status IN ('wait','in_progress') AND r.created_at < now() - {iv}
                       AND r.agent_task <> 'archive_bg'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='hr_stale' AND c.payload->>'request_id' = r.request_id::text)
                       AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x
                                       WHERE x.agent_task='archive_bg'
                                         AND x.agent_quest LIKE 'ANOMALY:' || r.request_id || '%')""")
                cur.execute(f"""
                    INSERT INTO agents_main.agent_coordination (agent, topic, action, payload)
                    SELECT 'archive_bg', 'archive_anomaly', 'htq_stale',
                           jsonb_build_object('task_id', t.task_id, 'status', t.status, 'claimed_by', t.claimed_by,
                              'created_at', t.created_at::text,
                              'age_hours', round(EXTRACT(EPOCH FROM (now() - t.created_at))/3600.0, 1),
                              'note', 'new/in_progress >24h — НЕ архивируем, эскалация (Z3)')
                      FROM agents_main.human_task_queue t
                     WHERE t.status IN ('new','in_progress') AND t.created_at < now() - {iv}
                       AND COALESCE(t.task_notes->>'backlog','false') <> 'true'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='htq_stale' AND c.payload->>'task_id' = t.task_id::text)""")
                result['htq_anomalies'] = cur.rowcount
                cur.execute(f"""
                    INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status)
                    SELECT t.task_id, 'archive_bg',
                           'ANOMALY:TASK:' || t.task_id || ' — задача в status=\'' || t.status ||
                           '\' с ' || t.created_at::date || ' (>24ч, claimed_by=' || COALESCE(t.claimed_by,'-') ||
                           '). Проверьте: зависла/не взята?' ,
                           'wait'
                      FROM agents_main.human_task_queue t
                     WHERE t.status IN ('new','in_progress') AND t.created_at < now() - {iv}
                       AND COALESCE(t.task_notes->>'backlog','false') <> 'true'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='htq_stale' AND c.payload->>'task_id' = t.task_id::text)
                       AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x
                                       WHERE x.agent_task='archive_bg'
                                         AND x.agent_quest LIKE 'ANOMALY:TASK:' || t.task_id || '%')""")
            # ---------- A-UNACK: ANOMALY hr_unacked (a6cd8fd4, GO 822b02d6) ----------
            # human_response есть, агент НЕ закрыл (agent_ack_at NULL), age>24h:
            # НЕ архивируем (гейт ШАГ A держит), эскалация coord + ANOMALY-строка.
            un_hr = (f"SELECT count(*) FROM agents_main.human_request r WHERE r.status IN ('done','replied') "
                     f"AND r.human_response IS NOT NULL AND r.agent_ack_at IS NULL "
                     f"AND r.created_at < now() - {iv} AND r.agent_task <> 'archive_bg' "
                     f"AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c WHERE c.action='hr_unacked' "
                     f"AND c.payload->>'request_id' = r.request_id::text)")
            result['hr_unacked_anomalies'] = _count(cur, un_hr)
            if not dry_run:
                cur.execute(f"""
                    INSERT INTO agents_main.agent_coordination (agent, topic, action, payload)
                    SELECT 'archive_bg', 'archive_anomaly', 'hr_unacked',
                           jsonb_build_object('request_id', r.request_id, 'status', r.status,
                              'created_at', r.created_at::text,
                              'age_hours', round(EXTRACT(EPOCH FROM (now() - r.created_at))/3600.0, 1),
                              'note', 'human_response есть, agent_ack_at NULL, >24h — агент не закрыл; НЕ архивируем')
                      FROM agents_main.human_request r
                     WHERE r.status IN ('done','replied')
                       AND r.human_response IS NOT NULL AND r.agent_ack_at IS NULL
                       AND r.created_at < now() - {iv}
                       AND r.agent_task <> 'archive_bg'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='hr_unacked' AND c.payload->>'request_id' = r.request_id::text)""")
                cur.execute(f"""
                    INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status)
                    SELECT r.task_id, 'archive_bg',
                           'ANOMALY:UNACKED:' || r.request_id || ' — ответ человека есть с ' || r.created_at::date ||
                           ' (>24ч), агент НЕ закрыл (agent_ack_at NULL). Автоархив НЕ трогает запись. Прочитать ответ + agent_ack_at=now().',
                           'wait'
                      FROM agents_main.human_request r
                     WHERE r.status IN ('done','replied')
                       AND r.human_response IS NOT NULL AND r.agent_ack_at IS NULL
                       AND r.created_at < now() - {iv}
                       AND r.agent_task <> 'archive_bg'
                       AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c
                                       WHERE c.action='hr_unacked' AND c.payload->>'request_id' = r.request_id::text)
                       AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x
                                       WHERE x.agent_task='archive_bg'
                                         AND x.agent_quest LIKE 'ANOMALY:UNACKED:' || r.request_id || '%')""")
            # ---------- ШАГ B: archived -> archive_* (в т.ч. ручные archived) ----------
            if dry_run:
                result['hr_moved_to_archive'] = _count(
                    cur, "SELECT count(*) FROM agents_main.human_request hr WHERE hr.status='archived' "
                         "AND NOT EXISTS (SELECT 1 FROM agents_main.archive_human_request a WHERE a.request_id=hr.request_id)")
                result['htq_moved_to_archive'] = _count(
                    cur, "SELECT count(*) FROM agents_main.human_task_queue htq WHERE htq.status='archived' "
                         "AND NOT EXISTS (SELECT 1 FROM agents_main.archive_human_task_queue a WHERE a.task_id=htq.task_id)")
            else:
                _insert_archive_row(cur, 'human_request', 'archive_human_request', 'hr', 'request_id',
                                    ['request_id', 'task_id', 'created_at', 'updated_at', 'finish_at',
                                     'agent_task', 'agent_quest', 'human_response', 'human_approved', 'status'])
                result['hr_moved_to_archive'] = cur.rowcount
                cur.execute("""
                    DELETE FROM agents_main.human_request
                     WHERE request_id IN (SELECT a.request_id FROM agents_main.archive_human_request a
                        WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='age_24h_or_manual')""")
                _insert_archive_row(cur, 'human_task_queue', 'archive_human_task_queue', 'htq', 'task_id',
                                    ['task_id', 'task', 'status', 'claimed_by', 'assigned_at', 'task_notes',
                                     'created_at', 'finished_at', 'priority'])
                result['htq_moved_to_archive'] = cur.rowcount
                cur.execute("""
                    DELETE FROM agents_main.human_task_queue
                     WHERE task_id IN (SELECT a.task_id FROM agents_main.archive_human_task_queue a
                        WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='age_24h_or_manual')""")
            # ---------- ШАГ C: чекпоинты (постоянная форма — по archive_*) ----------
            if dry_run:
                result['cp_moved_to_archive'] = _count(
                    cur, """SELECT count(*) FROM agents_main.analyst_daemon_queue q
                       WHERE q.request_type='checkpoint'
                         AND EXISTS (SELECT 1 FROM agents_main.archive_human_request hr
                                    WHERE hr.agent_task = q.request_payload->>'agent'
                                      AND hr.task_id = q.request_payload->>'task_id')
                         AND EXISTS (SELECT 1 FROM agents_main.archive_human_task_queue htq
                                    WHERE htq.task_id::text = q.request_payload->>'task_id')
                         AND NOT EXISTS (SELECT 1 FROM agents_main.archive_checkpoints a WHERE a.id = q.id)""")
            else:
                cur.execute("""
                    INSERT INTO agents_main.archive_checkpoints
                        (id, status, request_type, request_payload, response_payload, agent_reasoning,
                         created_at, updated_at, processed_by_model, archived_at, archive_reason, archived_by)
                    SELECT q.id, q.status, q.request_type, q.request_payload, q.response_payload,
                           q.agent_reasoning, q.created_at, q.updated_at, q.processed_by_model,
                           now(), 'gc_task_and_report_archived', 'bg_archive'
                      FROM agents_main.analyst_daemon_queue q
                     WHERE q.request_type='checkpoint'
                       AND EXISTS (SELECT 1 FROM agents_main.archive_human_request hr
                                  WHERE hr.agent_task = q.request_payload->>'agent'
                                    AND hr.task_id = q.request_payload->>'task_id')
                       AND EXISTS (SELECT 1 FROM agents_main.archive_human_task_queue htq
                                  WHERE htq.task_id::text = q.request_payload->>'task_id')
                       AND NOT EXISTS (SELECT 1 FROM agents_main.archive_checkpoints a WHERE a.id = q.id)""")
                result['cp_moved_to_archive'] = cur.rowcount
                cur.execute("""
                    DELETE FROM agents_main.analyst_daemon_queue
                     WHERE request_type='checkpoint'
                       AND id IN (SELECT a.id FROM agents_main.archive_checkpoints a
                          WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='gc_task_and_report_archived')""")
            # ---------- ЛОГ ПРОГОНА + КОНТРОЛЬ ----------
            cur.execute(
                "INSERT INTO agents_main.agent_coordination (agent, topic, action, payload) VALUES (%s, 'archive:startup', 'startup_archive', %s)",
                (agent_name, _to_jsonb(result)))
            result['live_archived_after'] = {
                'hr': _count(cur, "SELECT count(*) FROM agents_main.human_request WHERE status='archived'"),
                'htq': _count(cur, "SELECT count(*) FROM agents_main.human_task_queue WHERE status='archived'"),
            }
            result['archive_totals'] = {
                'hr': _count(cur, "SELECT count(*) FROM agents_main.archive_human_request"),
                'htq': _count(cur, "SELECT count(*) FROM agents_main.archive_human_task_queue"),
                'cp': _count(cur, "SELECT count(*) FROM agents_main.archive_checkpoints"),
            }
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — не роняем агента
            conn.rollback()
            result['status'] = 'ERROR'
            result['error'] = f'{type(exc).__name__}: {str(exc)[:500]}'
    if result['status'] == 'ERROR':
        logging.warning('startup archive FAILED (agent=%s, dry_run=%s): %s', agent_name, dry_run, result['error'])
    else:
        logging.info('startup archive OK (agent=%s dry_run=%s): %s', agent_name, dry_run,
                     {k: v for k, v in result.items() if not k.startswith(('ts', 'error'))})
    return result


def _to_jsonb(obj) -> 'dict':
    """Обёртка psycopg2.extras.Json (импорт лениво — модуль должен грузиться без psycopg2)."""
    from psycopg2.extras import Json
    return Json(obj)


if __name__ == '__main__':
    import argparse
    import json as _json
    parser = argparse.ArgumentParser(description='Автоархивация alfa (v3.3 в коде)')
    parser.add_argument('--dry-run', action='store_true', help='только подсчёты, без изменений данных')
    parser.add_argument('--agent', default='alfa')
    args = parser.parse_args()
    _res = run_startup_archive(None, args.agent, args.dry_run)
    print(_json.dumps(_res, ensure_ascii=False, indent=2))
