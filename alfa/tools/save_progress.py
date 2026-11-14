"""
Инструмент save_progress — сохранение текущего прогресса работы агента.

Вызывай этот инструмент после завершения каждого крупного этапа работы.

task_id берётся из env-переменной AGENT_TASK_ID (устанавливается main.py).

P1-5 (rev 1.2, 2026-09-22): импорт PG_CONFIG из СВОЕГО alfa.core.config
(был `from devops_agent.core.config import PG_CONFIG` — зависимость от
пакета чужого агента). Сбой записи в БД больше не глотается молча —
возвращается пометка в ответ (файл сохраняется в любом случае).
"""
import json
import os
from datetime import datetime, timezone
import uuid
import psycopg2

from alfa.core.config import AGENT_ROOT
from alfa.core.longlive_memory import write_checkpoint
SAVE_DIR = str(AGENT_ROOT / 'progress')


def save_progress(done: str, results: str, remaining: str) -> str:
    """
    Сохрани текущий прогресс работы. Вызывай этот инструмент после завершения
    каждого крупного этапа работы. Важно, сохраняй всю ту информацию, что поможет тебе выполнить задачу после внезапно перезапуска!

    Args:
        done: Что уже сделано
        results: Ключевые результаты (факты, числа, пути к файлам)
        remaining: Что осталось сделать

    Returns:
        1 строка с подтверждением сохранения.
    """
    # ---------- task_id ----------
    task_id = os.environ.get("AGENT_TASK_ID", "").strip()

    # ---------- build payload ----------
    progress = {
        "done": done,
        "results": results,
        "remaining": remaining,
        "saved_at": datetime.now().isoformat(),
    }

    # ---------- save to file ----------
    os.makedirs(SAVE_DIR, exist_ok=True)

    if task_id:
        safe_id = task_id.replace("-", "_")
        file_path = os.path.join(SAVE_DIR, f"progress_{safe_id}.json")
    else:
        short_id = uuid.uuid4().hex[:8]
        file_path = os.path.join(SAVE_DIR, f"progress_{short_id}.json")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

    # ---------- save to PostgreSQL (свой реестр — P1-5) ----------
    db_note = ""
    if task_id:
        try:
            from alfa.core.config import PG_CONFIG
            conn = psycopg2.connect(**PG_CONFIG)
            try:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO agents_main.artifacts
                       (task_id, artifact_type, name, content, created_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        task_id,
                        "progress",
                        "progress",
                        json.dumps(progress, ensure_ascii=False),
                        datetime.now(timezone.utc),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            db_note = f" | ⚠️ DB: {str(e)[:120]}"

    try:
        write_checkpoint({'task_id': task_id, 'done': [done], 'remaining': [remaining],
                          'key_facts': [results], 'artifacts': [file_path]})
    except Exception as exc:
        db_note += f' | checkpoint DB error: {exc}'

    # ---------- return: 1 line ----------
    file_size = os.path.getsize(file_path)
    return f"✓ saved → {file_path} ({file_size} bytes){db_note}"


# Metadata (для registry / docgen)
TOOL_NAME = "save_progress"
TOOL_ID = "save_progress"
