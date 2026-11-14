"""
AgentCheckpoint — сохранение состояний агента, чекпоинтов и суммаризаций.

Расширенная версия с поддержкой:
- Структурированных чекпоинтов (JSON)
- Сохранения суммаризаций в PostgreSQL и файловую систему
- Поддержки иерархической стратегии перезагрузки
"""
import psycopg2
from psycopg2 import pool
import json
import os
from datetime import datetime, timezone
import uuid
from contextlib import contextmanager
from gemma.core.config import PG_CONFIG, AGENT_ROOT, LONGLIVE_AGENT_NAME

CHECKPOINT_DIR = str(AGENT_ROOT / 'logs' / 'summaries')


class AgentCheckpoint:
    """Saves agent state to PostgreSQL for checkpoint/resume with hierarchical restart support."""

    def __init__(self, db_config: dict = None):
        # Use shared PG_CONFIG as default, allow override via parameter
        self.db_config = dict(PG_CONFIG)  # copy to avoid mutating shared dict
        if db_config:
            self.db_config.update(db_config)
        self._pool = None
        try:
            self._init_db()
            os.makedirs(CHECKPOINT_DIR, exist_ok=True)
        except Exception:
            self.close()
            raise

    @contextmanager
    def _get_conn(self):
        """Context manager for getting a connection from a pool."""
        if self._pool is None:
            self._pool = psycopg2.pool.SimpleConnectionPool(
                minconn=1,
                maxconn=5,
                **self.db_config
            )
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            try:
                if not conn.closed:
                    conn.rollback()
            finally:
                self._pool.putconn(conn, close=bool(conn.closed))

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Tasks
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ,
                    status TEXT,
                    original_prompt TEXT,
                    current_agent TEXT,
                    current_model TEXT,
                    metadata TEXT
                )
            """)

            # Messages (agent history)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    task_id TEXT,
                    timestamp TIMESTAMPTZ,
                    agent TEXT,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    token_metrics TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                )
            """)

            cursor.execute('ALTER TABLE agents_main.messages ADD COLUMN IF NOT EXISTS agent TEXT')

            # Artifacts (files, results, intermediate data)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id SERIAL PRIMARY KEY,
                    task_id TEXT,
                    artifact_type TEXT,
                    name TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                )
            """)

            # Summaries (structured checkpoint data for hierarchical restart)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    id SERIAL PRIMARY KEY,
                    task_id TEXT,
                    mode TEXT,
                    context_percent FLOAT,
                    last_step INTEGER,
                    summary_json JSONB,
                    file_path TEXT,
                    created_at TIMESTAMPTZ,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                )
            """)

            conn.commit()

    def create_task(self, prompt: str, agent_name: str, model: str) -> str:
        """Create a new task, return task_id."""
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agents_main.tasks (task_id, created_at, updated_at, status, 
                                 original_prompt, current_agent, current_model)
                VALUES (%s, %s, %s, 'running', %s, %s, %s)
            """, (task_id, now, now, prompt, agent_name, model))
            conn.commit()
        return task_id

    def save_message(self, task_id: str, role: str, content: str,
                     tool_calls: list = None, tool_call_id: str = None,
                     token_metrics: dict = None, agent: str = None):
        """Save message to history (with token metrics)."""
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            metrics_json = json.dumps(token_metrics, ensure_ascii=False) if token_metrics else None
            cursor.execute("""
                INSERT INTO agents_main.messages (task_id, timestamp, role, content, tool_calls, tool_call_id, token_metrics, agent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (task_id, now, role, str(content),
                  (tool_calls if isinstance(tool_calls, str) else json.dumps(tool_calls)) if tool_calls else None,
                  tool_call_id, metrics_json, agent or LONGLIVE_AGENT_NAME))
            # Single connection for both INSERT and UPDATE
            cursor.execute("UPDATE tasks SET updated_at = %s WHERE task_id = %s", (now, task_id))
            conn.commit()

    def load_task_history(self, task_id: str) -> list:
        """Load full message history for a task."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content, tool_calls, timestamp 
                FROM agents_main.messages 
                WHERE task_id = %s 
                ORDER BY "timestamp", id
            """, (task_id,))
            messages = []
            for role, content, tool_calls_json, timestamp in cursor.fetchall():
                messages.append({
                    "role": role,
                    "content": content,
                    "tool_calls": json.loads(tool_calls_json) if tool_calls_json else None,
                    "timestamp": timestamp
                })
            return messages

    def save_artifact(self, task_id: str, artifact_type: str, name: str, content: str):
        """Save artifact (file, report, data)."""
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agents_main.artifacts (task_id, artifact_type, name, content, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (task_id, artifact_type, name, content, now))
            conn.commit()

    def update_task_status(self, task_id: str, status: str, model: str = None):
        """Update task status."""
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if model:
                cursor.execute("""
                    UPDATE agents_main.tasks SET status = %s, updated_at = %s, current_model = %s
                    WHERE task_id = %s
                """, (status, now, model, task_id))
            else:
                cursor.execute("""
                    UPDATE agents_main.tasks SET status = %s, updated_at = %s
                    WHERE task_id = %s
                """, (status, now, task_id))
            conn.commit()

    def list_tasks(self, status: str = None) -> list:
        """List tasks, optionally filtered by status."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("""
                    SELECT task_id, created_at, status, original_prompt, current_model
                    FROM agents_main.tasks WHERE status = %s ORDER BY updated_at DESC
                """, (status,))
            else:
                cursor.execute("""
                    SELECT task_id, created_at, status, original_prompt, current_model
                    FROM agents_main.tasks ORDER BY updated_at DESC
                """)
            tasks = cursor.fetchall()
            return tasks

    # ============================================================
    # НОВЫЕ МЕТОДЫ: Сохранение чекпоинтов и суммаризаций
    # ============================================================

    def save_summary(self, task_id: str, summary: dict, context_percent: float = None) -> str:
        """
        Сохраняет структурированный чекпоинт в PostgreSQL и файл.
        
        Args:
            task_id: ID задачи
            summary: dict с данными чекпоинта
            context_percent: процент использования контекста
            
        Returns:
            Путь к сохраненному файлу
        """
        now = datetime.now(timezone.utc)
        
        # Сохраняем в БД
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agents_main.summaries (task_id, mode, context_percent, last_step, summary_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                task_id,
                summary.get('mode', 'unknown'),
                context_percent,
                summary.get('last_step', 0),
                json.dumps(summary, ensure_ascii=False, default=str),
                now
            ))
            conn.commit()
        
        # Сохраняем в файл для надежности
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_path = os.path.join(
            CHECKPOINT_DIR,
            f"summary_{task_id[:8]}_{timestamp_str}.json"
        )
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        
        # Обновляем file_path в БД — без ORDER BY + LIMIT в UPDATE
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agents_main.summaries 
                SET file_path = %s 
                WHERE id = (
                    SELECT id FROM agents_main.summaries 
                    WHERE task_id = %s AND summary_json->>'mode' = %s 
                    ORDER BY created_at DESC LIMIT 1
                )
            """, (file_path, task_id, summary.get('mode', 'unknown')))
            conn.commit()
        
        return file_path

    def create_structured_checkpoint(self, task_id: str, history: list, 
                                     last_step: int, mode: str = "longlive", next_steps: list = None) -> dict:
        """
        Создает структурированный JSON-чекпоинт.
        
        Args:
            task_id: ID задачи
            history: полный список сообщений
            last_step: номер последнего шага
            mode: режим перезагрузки (continue/trim/progressive_summarize)
            
        Returns:
            dict с данными чекпоинта
        """
        completed = []
        in_progress = []
        key_findings = []
        blockers = []
        next_steps = list(next_steps or [])
        
        # Парсим историю и извлекаем информацию
        for i, msg in enumerate(history):
            content = str(msg.get('content', ''))
            role = msg.get('role', '')
            
            if role == 'assistant':
                if any(word in content.lower() for word in ('next step', 'remaining', 'следующ', 'осталось')):
                    next_steps.append(content[:500])
                # Ищем ключевые паттерны в ответе агента
                if any(keyword in content.lower() for keyword in ['completed', 'done', 'finished', 'завершено']):
                    completed.append(content[:500])
                elif any(keyword in content.lower() for keyword in ['error', 'fail', 'ошибка', 'проблема']):
                    blockers.append(content[:300])
                elif any(keyword in content.lower() for keyword in ['path', 'file', 'server', 'id:']):
                    key_findings.append(content[:200])
                else:
                    in_progress.append(content[:300])
        
        return {
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "last_step": last_step,
            "completed": completed[:10],  # максимум 10 завершенных
            "in_progress": in_progress[:10],
            "key_findings": key_findings[:10],
            "blockers": blockers[:5],
            "next_steps": next_steps[:5],
            "mode": mode
        }

    def load_last_summary(self, task_id: str) -> dict:
        """
        Загружает последнюю суммаризацию для задачи.
        
        Args:
            task_id: ID задачи
            
        Returns:
            dict с данными суммаризации или None
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT summary_json, file_path, created_at
                FROM agents_main.summaries 
                WHERE task_id = %s 
                ORDER BY created_at DESC 
                LIMIT 1
            """, (task_id,))
            row = cursor.fetchone()
            
            if row:
                return {
                    "summary": json.loads(row[0]) if isinstance(row[0], str) else row[0],
                    "file_path": row[1],
                    "created_at": row[2]
                }
            return None

    def find_running_task(self, agent):
        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT task_id FROM agents_main.tasks WHERE current_agent=%s AND status='running' ORDER BY updated_at DESC LIMIT 1", (agent,))
                row = cursor.fetchone()
                return row[0] if row else None

    def close(self):
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None

    def load_original_task(self, task_id):
        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT original_prompt FROM agents_main.tasks WHERE task_id=%s', (task_id,))
                row = cursor.fetchone()
                text = row[0] if row and row[0] else ''
                # Legacy tasks stored the entire bootstrap. Keep only its task section.
                for marker in ('## ТЕКУЩАЯ ЗАДАЧА', 'ТЕКУЩАЯ ЗАДАЧА:'):
                    if marker in text:
                        text = text.rsplit(marker, 1)[1].strip()
                        break
                return text
