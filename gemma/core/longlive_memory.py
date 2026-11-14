import os
import sys
import json
import uuid
from datetime import datetime, timezone

# Автономность: при запуске main.py как скрипта пакет alfa не в sys.path
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

import yaml
import psycopg2

from gemma.core.config import PG_CONFIG, LONGLIVE_BOOTSTRAP_PROMPT, LONGLIVE_AGENT_NAME


def load_bootstrap_prompt(task_description: str = "") -> str:
    """Читает якорный промт из локального YAML и подставляет текущую задачу."""
    with open(LONGLIVE_BOOTSTRAP_PROMPT, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    tpl = cfg.get("system_fixed", "Ты — долгоживущий автономный ИИ-агент аналитик.")
    from gemma.core.config import AGENT_ROOT
    tpl = tpl.replace('{agent_root}', str(AGENT_ROOT))
    if "{task_description}" in tpl:
        return tpl.replace("{task_description}", task_description.strip() or "(свободный режим: автономная работа по памяти и human_request)")
    return tpl + "\n\nТЕКУЩАЯ ЗАДАЧА:\n" + (task_description.strip() or "(свободный режим)")


def _q(sql: str, params: tuple = ()):
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def load_last_checkpoint(limit: int = 1) -> tuple:
    """Only attributable own checkpoints. Never borrow another agent's identity."""
    rows = _q(
        """SELECT q.request_payload, q.agent_reasoning, q.request_type, q.created_at
           FROM analyst_daemon_queue q
           WHERE q.request_type IN ('checkpoint','report')
             AND (q.request_payload->>'agent' = %s OR
                  (q.request_payload->>'agent' IS NULL AND EXISTS (
                      SELECT 1 FROM agents_main.tasks t
                      WHERE t.task_id = q.request_payload->>'task_id' AND t.current_agent = %s)))
           ORDER BY q.created_at DESC LIMIT %s""",
        (LONGLIVE_AGENT_NAME, LONGLIVE_AGENT_NAME, max(1, min(limit, 5))))
    return rows, False


def load_pending_human_requests(limit: int = 10) -> list:
    """Незакрытые/недавние запросы и ответы человека.
    Словарь 69cf6a54 + GO 24.09 (9f642501): wait/new (новые) + replied/done/archived
    в 24ч-окне (archived — скриптом человека через 24ч после done).
    Окно по GREATEST(created_at, COALESCE(finish_at, created_at)): finish_at НЕГОВЕРЯН
    (live-факт 24.09: 30 строк с пачечным finish_at=23.09 10:01:10).
    """
    return _q(
        """SELECT request_id, agent_quest, human_response, status, human_approved, created_at
           FROM human_request
           WHERE (status IN ('wait','new')
               OR (status IN ('replied','done','archived')
                   AND GREATEST(created_at, COALESCE(finish_at, created_at)) > NOW() - INTERVAL '24 hours')
               OR (human_response IS NOT NULL AND agent_ack_at IS NULL))
             AND (agent_task = %s OR task_id IN (SELECT task_id FROM agents_main.tasks WHERE current_agent = %s))
           ORDER BY created_at DESC LIMIT %s""",
        (LONGLIVE_AGENT_NAME, LONGLIVE_AGENT_NAME, limit),
    )


def auto_ack_seen_responses() -> int:
    # AUTO-ACK (rest#13 alfa, GO 2da41695 variant C; gemma apply c476f4e3 12.11):
    # core sets agent_ack_at=NOW() for MY human_request rows that have human_response
    # but no ack. Called from build_db_memory_block() — the moment the agent READS the
    # response into its prompt (read = ack). Closes root cause "53+ HR unacked forever
    # + ANOMALY:UNACKED spam" (RAG 07cd4e6e; startup_archive A-UNACK).
    # Risk "agent not satisfied" accepted by human (re-ask = separate mechanism).
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE human_request
               SET agent_ack_at = NOW()
               WHERE human_response IS NOT NULL
                 AND agent_ack_at IS NULL
                 AND (agent_task = %s OR task_id IN (SELECT task_id FROM agents_main.tasks WHERE current_agent = %s))""",
            (LONGLIVE_AGENT_NAME, LONGLIVE_AGENT_NAME),
        )
        n = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        n = 0
    finally:
        conn.close()
    return n


def _items(value):
    return value if isinstance(value, list) else [value]


def _clip(s, n: int = 1500):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f" ...[обрезано {len(s) - n} симв.]"


def write_checkpoint(payload: dict, reasoning: str = "") -> str:
    """
    Запись чекпоинта в analyst_daemon_queue (долгосрочная память).
    payload: {done:[...], remaining:[...], key_facts:[...], artifacts:[...], cycle:"..."}

    P0-2 (rev 1.2): поле `agent` добавляется АВТОМАТИЧЕСКИ
    (= LONGLIVE_AGENT_NAME) — фильтр в load_last_checkpoint перестаёт
    зависеть от дисциплины агента. processed_by_model тоже = имя агента.
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = dict(payload)
    payload["agent"] = LONGLIVE_AGENT_NAME
    payload.setdefault("task_id", os.environ.get("AGENT_TASK_ID", ""))  # авто-поле (анти-амнезия)
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO analyst_daemon_queue
               (id, status, request_type, request_payload, agent_reasoning, created_at, updated_at, processed_by_model)
               VALUES (%s, 'done', 'checkpoint', %s::jsonb, %s, %s, %s, %s)""",
            (cid, json.dumps(payload, ensure_ascii=False), reasoning, now, now, LONGLIVE_AGENT_NAME),
        )
        conn.commit()
    finally:
        conn.close()
    return f"checkpoint saved: {cid}"


def build_db_memory_block(compact: bool = False) -> str:
    """Блок 'ПАМЯТЬ ИЗ БД' для промпта рестарта. Компактно: обрезка длинных полей.

    compact=True (loop-guard 7881bfef, L5): сильная обрезка для oversized-bootstrap."""
    n_acked = auto_ack_seen_responses()
    if n_acked:
        print(f'[longlive_memory] AUTO-ACK: {n_acked} human_response acked (agent_ack_at=NOW())')
    _clip_len = {'done': 100, 'remaining': 100, 'key_facts': 250, 'artifacts': 100} if compact \
        else {'done': 200, 'remaining': 200, 'key_facts': 200, 'artifacts': 150}
    _n_items = 4 if compact else 6
    lines = []
    try:
        cps, possibly_foreign = load_last_checkpoint()
        if cps:
            lines.append(f"### Последний чекпоинт ({len(cps)} шт.):")
            if possibly_foreign:
                lines.append("ВНИМАНИЕ: чекпоинт БЕЗ поля agent — ВОЗМОЖЕН ЧУЖОЙ (P0-2). Сверься по артефактам/идентичности перед тем, как продолжать.")
            for payload, reasoning, rtype, ts in cps:
                p = payload if isinstance(payload, dict) else {}
                agent_tag = p.get("agent", "(нет)")
                lines.append(f"- [{rtype}] agent={agent_tag} {ts}")
                if p.get("done"):
                    lines.append("  done: " + "; ".join(_clip(str(x), _clip_len["done"]) for x in _items(p["done"])[:_n_items]))
                if p.get("remaining"):
                    lines.append("  remaining: " + "; ".join(_clip(str(x), _clip_len["remaining"]) for x in _items(p["remaining"])[:_n_items]))
                if p.get("key_facts"):
                    lines.append("  key_facts: " + "; ".join(_clip(str(x), _clip_len["key_facts"]) for x in _items(p["key_facts"])[:_n_items]))
                if p.get("artifacts"):
                    lines.append("  artifacts: " + "; ".join(_clip(str(x), _clip_len["artifacts"]) for x in _items(p["artifacts"])[:_n_items]))
                if p.get('recent_steps') and not compact:
                    lines.append('  recent_steps: ' + _clip(json.dumps(p['recent_steps'][-8:], ensure_ascii=False), 6000))
                if p.get('restart_reason'):
                    lines.append("  restart_reason: " + _clip(p.get('restart_reason'), 250 if compact else 500))
                if reasoning:
                    lines.append("  reasoning: " + _clip(reasoning, 200 if compact else 400))
        else:
            lines.append("### Последний чекпоинт: НЕТ (возможно, первый запуск).")
    except Exception as e:
        lines.append(f"### Последний чекпоинт: ОШИБКА ЧТЕНИЯ {e}")

    try:
        hrs = load_pending_human_requests(limit=1 if compact else 10)
        if hrs:
            lines.append(f"### human_request (последние {len(hrs)}):")
            for rid, quest, resp, status, appr, ts in hrs:
                lines.append(f"- {rid} [{status}] {ts}: quest={_clip(quest, 200 if compact else 300)}")
                if resp:
                    lines.append(f"    human_response={_clip(resp, 200 if compact else 600)}")
        else:
            lines.append("### human_request: пусто.")
    except Exception as e:
        lines.append(f"### human_request: ОШИБКА ЧТЕНИЯ {e}")

    from gemma.core.config import AGENT_ROOT
    recovery_files = sorted((AGENT_ROOT / 'logs').glob('cycle_recovery_*.json'), reverse=True)
    if recovery_files and not compact:
        lines.append('### Local recovery (DB write previously failed): ' +
                     _clip(recovery_files[0].read_text(encoding='utf-8'), 6000))
    return "\n".join(lines)


def build_longlive_restart_prompt(task_description: str = "", restart_number: int = 0, compact: bool = False) -> str:
    """Якорный промт + память из БД. Возвращает полный текст нового 'первичного' промпта.

    compact=True (loop-guard 7881bfef, L5): память усечена; используется в main.py,
    когда обычный bootstrap-промпт не помещается в контекстный бюджет."""
    boot = load_bootstrap_prompt(task_description)
    mem = build_db_memory_block(compact=compact)
    header = (
        f"АГЕНТ ПЕРЕЗАГРУЖЕН — РЕЖИМ LONGLIVE (рестарт #{restart_number})\n"
        f"Время: {datetime.now(timezone.utc).isoformat()}\n"
        f"Ниже — ТВОЙ ПЕРВИЧНЫЙ ПРОМТ и свежая ПАМЯТЬ ИЗ БД. История шагов намеренно не вшита.\n"
        f"Первый шаг: перечитай свой план-файл и продолжай с чекпоинта.\n\n"
    )
    return header + boot + "\n\n═══════════════════════════════════\nПАМЯТЬ ИЗ БД (свежее состояние):\n" + mem + "\n" + "═" * 39
