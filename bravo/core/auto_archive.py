# -*- coding: utf-8 -*-
"""auto_archive.py (bravo core) — АВТОАРХИВАЦИЯ при старте агента.

Директива человека (1ed0ea3a, rest#7, 24.09 14:1x): «вместо фонового процесса —
при старте агента (сначала alfa, потом bravo) выполнять автоархивацию старых
задач и чекпоинтов». Это закрывает архитектурную ошибку 69cf6a54/1090f61e:
раньше archived-строки копились в live-таблицах (память раздувалась ~20k токенов),
потому что перенос в archive_* никто не делал.

ЛОГИКА (= сценарий v3.3, план/1090f61e_archive_scenario_v3_3_bravo.sql):
  A. перевод в 'archived' (24ч после done): human_request (done/replied) +
     human_task_queue (done). Аномалии wait/in_progress/new >24ч НЕ архивируются —
     лог agent_coordination + ANOMALY-запрос человеку (idempotent).
  B. перенос ВСЕХ archived из live в archive_human_request / archive_human_task_queue
     + DELETE из live.
  C. чекпоинты: ТОЛЬКО точные связи, РОДИТЕЛИ ИЩЕМ В archive_* (П1b-фикс: после B
     live чист, поэтому live-форма мёртва в steady-state). Формула человека:
     «И задача человека (htq) И отчёт (hr) = archived».
Порядок A->B->C. Каждый блок = отдельная транзакция (откат одного не ломает других).
Идемпотентно: NOT EXISTS по PK + точные связки.

БЕЗОПАСНОСТЬ:
  - НЕ падает агент: любые исключения ловятся, логируются, возврат {} (агент живёт).
  - dry_run=True: только SELECT (счётчики), ничего не пишет.
  - Лог в logging + 1 запись в agent_coordination (topic='auto_archive', agent=имя)
    — видно в lives-якорях и журнале.
Вызов: core/auto_archive.py run_auto_archive(agent_name, dry_run=False)
"""
import json
import logging
from datetime import datetime, timezone

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import psycopg2
from psycopg2.extras import Json

from bravo.core.config import PG_CONFIG

# Блоки SQL (v3.3). Параметра нет — статика; идемпотентность внутри.
_SQL_A1 = (
    "UPDATE agents_main.human_request SET status='archived', updated_at=now() "
    "WHERE status IN ('done','replied') "
    "AND human_response IS NOT NULL "
    "AND agent_ack_at IS NOT NULL "
    "AND GREATEST(COALESCE(finish_at, created_at), COALESCE(updated_at, created_at)) < now() - interval '24 hours'"
)
_SQL_A1_ANO_COORD = (
    "INSERT INTO agents_main.agent_coordination (agent, topic, action, payload) "
    "SELECT 'archive_bg', 'archive_anomaly', 'hr_stale', "
    "jsonb_build_object('request_id', r.request_id, 'status', r.status, "
    "'created_at', r.created_at::text, 'age_hours', "
    "round(EXTRACT(EPOCH FROM (now() - r.created_at))/3600.0, 1), "
    "'note', 'wait/in_progress >24h — НЕ архивируем, эскалация') "
    "FROM agents_main.human_request r "
    "WHERE r.status IN ('wait','in_progress') AND r.created_at < now() - interval '24 hours' "
    "AND r.agent_task <> 'archive_bg' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c "
    "WHERE c.action='hr_stale' AND c.payload->>'request_id' = r.request_id::text)"
)
_SQL_A1_ANO_HR = (
    "INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status) "
    "SELECT r.task_id, 'archive_bg', "
    "'ANOMALY:HR:' || r.request_id || ' [status:' || r.status || '] ' || r.created_at::date || ' >24h no-move. Check: unread or stuck', 'wait' "
    "FROM agents_main.human_request r "
    "WHERE r.status IN ('wait','in_progress') AND r.created_at < now() - interval '24 hours' "
    "AND r.agent_task <> 'archive_bg' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x WHERE x.agent_task='archive_bg' "
    "AND x.agent_quest LIKE 'ANOMALY:HR:' || r.request_id || '%')"
)
_SQL_A1_UNACKED_COORD = (
    "INSERT INTO agents_main.agent_coordination (agent, topic, action, payload) "
    "SELECT 'archive_bg', 'archive_anomaly', 'hr_unacked', "
    "jsonb_build_object('request_id', r.request_id, 'status', r.status, "
    "'created_at', r.created_at::text, 'age_hours', "
    "round(EXTRACT(EPOCH FROM (now() - r.created_at))/3600.0, 1), "
    "'note', 'human_response есть, agent_ack_at NULL, >24h — агент не закрыл; НЕ архивируем (a6cd8fd4)') "
    "FROM agents_main.human_request r "
    "WHERE r.status IN ('done','replied') AND r.human_response IS NOT NULL AND r.agent_ack_at IS NULL "
    "AND r.created_at < now() - interval '24 hours' AND r.agent_task <> 'archive_bg' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c "
    "WHERE c.action='hr_unacked' AND c.payload->>'request_id' = r.request_id::text)"
)
_SQL_A1_UNACKED_HR = (
    "INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status) "
    "SELECT r.task_id, 'archive_bg', "
    "'ANOMALY:UNACKED:' || r.request_id || ' — ответ человека есть (>24ч), агент НЕ закрыл (agent_ack_at NULL). Автоархив НЕ трогает. Прочитать ответ + agent_ack_at=now().', 'wait' "
    "FROM agents_main.human_request r "
    "WHERE r.status IN ('done','replied') AND r.human_response IS NOT NULL AND r.agent_ack_at IS NULL "
    "AND r.created_at < now() - interval '24 hours' AND r.agent_task <> 'archive_bg' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c "
    "WHERE c.action='hr_unacked' AND c.payload->>'request_id' = r.request_id::text) "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x WHERE x.agent_task='archive_bg' "
    "AND x.agent_quest LIKE 'ANOMALY:UNACKED:' || r.request_id || '%')"
)
_SQL_A2 = (
    "UPDATE agents_main.human_task_queue SET status='archived' "
    "WHERE status='done' AND COALESCE(finished_at, created_at) < now() - interval '24 hours'"
)
_SQL_A2_ANO_COORD = (
    "INSERT INTO agents_main.agent_coordination (agent, topic, action, payload) "
    "SELECT 'archive_bg', 'archive_anomaly', 'htq_stale', "
    "jsonb_build_object('task_id', t.task_id, 'status', t.status, 'claimed_by', t.claimed_by, "
    "'created_at', t.created_at::text, 'age_hours', "
    "round(EXTRACT(EPOCH FROM (now() - t.created_at))/3600.0, 1), "
    "'note', 'new/in_progress >24h — НЕ архивируем, эскалация (Z3)') "
    "FROM agents_main.human_task_queue t "
    "WHERE t.status IN ('new','in_progress') AND t.created_at < now() - interval '24 hours' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.agent_coordination c "
    "WHERE c.action='htq_stale' AND c.payload->>'task_id' = t.task_id::text)"
)
_SQL_A2_ANO_HR = (
    "INSERT INTO agents_main.human_request (task_id, agent_task, agent_quest, status) "
    "SELECT t.task_id, 'archive_bg', "
    "'ANOMALY:TASK:' || t.task_id || ' [status:' || t.status || '] ' || t.created_at::date || ' >24h claimed_by:' || COALESCE(t.claimed_by,'-') || ' . Check: stuck or unclaimed', 'wait' "
    "FROM agents_main.human_task_queue t "
    "WHERE t.status IN ('new','in_progress') AND t.created_at < now() - interval '24 hours' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.human_request x WHERE x.agent_task='archive_bg' "
    "AND x.agent_quest LIKE 'ANOMALY:TASK:' || t.task_id || '%')"
)
_SQL_B1 = (
    "INSERT INTO agents_main.archive_human_request "
    "(request_id, task_id, created_at, updated_at, finish_at, agent_task, agent_quest, "
    "human_response, human_approved, status, agent_ack_at, archived_at, archive_reason, archived_by) "
    "SELECT hr.request_id, hr.task_id, hr.created_at, hr.updated_at, hr.finish_at, "
    "hr.agent_task, hr.agent_quest, hr.human_response, hr.human_approved, hr.status, hr.agent_ack_at, "
    "now(), 'age_24h_or_manual', 'auto_archive_start' "
    "FROM agents_main.human_request hr WHERE hr.status='archived' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.archive_human_request a WHERE a.request_id=hr.request_id)"
)
_SQL_B1_DEL = (
    "DELETE FROM agents_main.human_request WHERE request_id IN "
    "(SELECT a.request_id FROM agents_main.archive_human_request a "
    "WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='age_24h_or_manual')"
)
_SQL_B3 = (
    "INSERT INTO agents_main.archive_human_task_queue "
    "(task_id, task, status, claimed_by, assigned_at, task_notes, created_at, finished_at, "
    "priority, project, tags, archived_at, archive_reason, archived_by) "
    "SELECT htq.task_id, htq.task, htq.status, htq.claimed_by, htq.assigned_at, htq.task_notes, "
    "htq.created_at, htq.finished_at, htq.priority, htq.project, htq.tags, now(), 'age_24h_or_manual', 'auto_archive_start' "
    "FROM agents_main.human_task_queue htq WHERE htq.status='archived' "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.archive_human_task_queue a WHERE a.task_id=htq.task_id)"
)
_SQL_B3_DEL = (
    "DELETE FROM agents_main.human_task_queue WHERE task_id IN "
    "(SELECT a.task_id FROM agents_main.archive_human_task_queue a "
    "WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='age_24h_or_manual')"
)
_SQL_C = (
    "INSERT INTO agents_main.archive_checkpoints "
    "(id, status, request_type, request_payload, response_payload, agent_reasoning, "
    "created_at, updated_at, processed_by_model, archived_at, archive_reason, archived_by) "
    "SELECT q.id, q.status, q.request_type, q.request_payload, q.response_payload, "
    "q.agent_reasoning, q.created_at, q.updated_at, q.processed_by_model, "
    "now(), 'gc_task_and_report_archived', 'auto_archive_start' "
    "FROM agents_main.analyst_daemon_queue q WHERE q.request_type='checkpoint' "
    "AND EXISTS (SELECT 1 FROM agents_main.archive_human_request hr "
    "WHERE hr.agent_task = q.request_payload->>'agent' AND hr.task_id = q.request_payload->>'task_id') "
    "AND EXISTS (SELECT 1 FROM agents_main.archive_human_task_queue htq "
    "WHERE htq.task_id::text = q.request_payload->>'task_id') "
    "AND NOT EXISTS (SELECT 1 FROM agents_main.archive_checkpoints a WHERE a.id = q.id)"
)
_SQL_C_DEL = (
    "DELETE FROM agents_main.analyst_daemon_queue WHERE request_type='checkpoint' AND id IN "
    "(SELECT a.id FROM agents_main.archive_checkpoints a "
    "WHERE a.archived_at > now() - interval '1 hour' AND a.archive_reason='gc_task_and_report_archived')"
)

# dry-run SELECT-аналоги (счётчики кандидатов)
_DRY = {
    "a1": "SELECT count(*) FROM human_request WHERE status IN ('done','replied') AND human_response IS NOT NULL AND agent_ack_at IS NOT NULL AND GREATEST(COALESCE(finish_at, created_at), COALESCE(updated_at, created_at)) < now() - interval '24 hours'",
    "a2": "SELECT count(*) FROM human_task_queue WHERE status='done' AND COALESCE(finished_at, created_at) < now() - interval '24 hours'",
    "b_hr": "SELECT count(*) FROM human_request WHERE status='archived' AND NOT EXISTS (SELECT 1 FROM archive_human_request a WHERE a.request_id=human_request.request_id)",
    "b_htq": "SELECT count(*) FROM human_task_queue WHERE status='archived' AND NOT EXISTS (SELECT 1 FROM archive_human_task_queue a WHERE a.task_id=human_task_queue.task_id)",
    "c_cp": ("SELECT count(*) FROM analyst_daemon_queue q WHERE q.request_type='checkpoint' "
             "AND EXISTS (SELECT 1 FROM archive_human_request hr WHERE hr.agent_task=q.request_payload->>'agent' AND hr.task_id=q.request_payload->>'task_id') "
             "AND EXISTS (SELECT 1 FROM archive_human_task_queue htq WHERE htq.task_id::text=q.request_payload->>'task_id') "
             "AND NOT EXISTS (SELECT 1 FROM archive_checkpoints a WHERE a.id=q.id)"),
}


def _run_block(conn, sql):
    """Один блок = отдельная транзакция. Возвращает rowcount (или -1 при SELECT)."""
    cur = conn.cursor()
    cur.execute(sql)
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def run_auto_archive(agent_name: str, dry_run: bool = False) -> dict:
    """Автоархивация при старте. НЕ бросает исключений (агент должен жить)."""
    res = {"agent": agent_name, "dry_run": dry_run, "ts": datetime.now(timezone.utc).isoformat(),
           "blocks": {}, "ok": False, "error": None}
    conn = None
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        cur = conn.cursor()
        if dry_run:
            for k, sql in _DRY.items():
                cur.execute(sql)
                res["blocks"][k] = cur.fetchone()[0]
            cur.close()
            res["ok"] = True
            logging.info("auto_archive [%s] DRY-RUN: %s", agent_name, res["blocks"])
            _journal(agent_name, res, conn)
            return res

        # A
        res["blocks"]["a1"] = _run_block(conn, _SQL_A1)
        res["blocks"]["a1_unacked"] = _run_block(conn, _SQL_A1_UNACKED_COORD) + _run_block(conn, _SQL_A1_UNACKED_HR)
        res["blocks"]["a1_anom"] = _run_block(conn, _SQL_A1_ANO_COORD) + _run_block(conn, _SQL_A1_ANO_HR)
        res["blocks"]["a2"] = _run_block(conn, _SQL_A2)
        res["blocks"]["a2_anom"] = _run_block(conn, _SQL_A2_ANO_COORD) + _run_block(conn, _SQL_A2_ANO_HR)
        # B
        b1 = _run_block(conn, _SQL_B1)
        d1 = _run_block(conn, _SQL_B1_DEL)
        b3 = _run_block(conn, _SQL_B3)
        d3 = _run_block(conn, _SQL_B3_DEL)
        res["blocks"]["b_hr"] = b1
        res["blocks"]["b_htq"] = b3
        res["blocks"]["b_deleted"] = d1 + d3
        # C
        c1 = _run_block(conn, _SQL_C)
        c2 = _run_block(conn, _SQL_C_DEL)
        res["blocks"]["c_cp"] = c1
        res["blocks"]["c_deleted"] = c2
        cur.close()
        res["ok"] = True
        logging.info("auto_archive [%s] DONE: %s", agent_name, res["blocks"])
        _journal(agent_name, res, conn)
        return res
    except Exception as e:  # noqa: BLE001 — агент НЕ падает из-за архивации
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {e}"
        logging.exception("auto_archive [%s] FAILED (агент продолжает): %s", agent_name, e)
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return res
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _journal(agent_name: str, res: dict, conn):
    """1 запись в agent_coordination (видно в lives-якорях/журнале). Идемпотентность не нужна — это лог."""
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agents_main.agent_coordination (agent, topic, action, payload) VALUES (%s,%s,%s,%s)",
            (agent_name, 'auto_archive', 'startup_run', Json(res)),
        )
        conn.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001
        logging.warning("auto_archive journal FAILED: %s", e)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--agent', default='bravo')
    a = ap.parse_args()
    out = run_auto_archive(a.agent, dry_run=a.dry_run)
    print(json.dumps(out, ensure_ascii=False))
