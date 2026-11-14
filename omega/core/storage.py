"""Durable local SQLite memory; short transactions, WAL, and online backups."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .config import SQLITE_PATH

def now():
    return datetime.now(timezone.utc).isoformat()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, original_prompt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(task_id),
 created_at TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
 tool_calls TEXT, tool_call_id TEXT, token_metrics TEXT, model TEXT);
CREATE INDEX IF NOT EXISTS messages_task_id ON messages(task_id,id);
CREATE TABLE IF NOT EXISTS checkpoints (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(task_id),
 created_at TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(task_id),
 created_at TEXT NOT NULL, name TEXT NOT NULL, path TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS human_requests (
 request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'new', request TEXT NOT NULL, response TEXT);
CREATE TABLE IF NOT EXISTS agent_settings (
 setting_key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS process_jobs (
 job_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, process_created REAL,
 started_at TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL, log_path TEXT NOT NULL,
 status TEXT NOT NULL, exit_code INTEGER, finished_at TEXT);
'''

class SQLiteStore:
    def __init__(self, path=None):
        self.path = Path(path or SQLITE_PATH).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self, readonly=False):
        conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5) if readonly else sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        if not readonly:
            conn.execute('PRAGMA synchronous=FULL')
        try:
            yield conn
            if not readonly:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resume_or_create(self, description=''):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM tasks WHERE status='running' ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row:
                return row['task_id'], description or row['original_prompt']
            task_id = str(uuid.uuid4())
            conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (task_id, now(), now(), 'running', description))
            return task_id, description

    def save_message(self, task_id, role, content, tool_calls=None, tool_call_id=None, token_metrics=None, model=None):
        with self.connection() as conn:
            conn.execute('''INSERT INTO messages(task_id,created_at,role,content,tool_calls,tool_call_id,token_metrics,model)
                            VALUES (?,?,?,?,?,?,?,?)''',
                         (task_id, now(), role, str(content), json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                          tool_call_id, json.dumps(token_metrics) if token_metrics else None, model))
            conn.execute('UPDATE tasks SET updated_at=? WHERE task_id=?', (now(), task_id))

    def latest_checkpoint(self, task_id=None):
        with self.connection(readonly=True) as conn:
            row = conn.execute('SELECT payload FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone() if task_id else conn.execute('SELECT payload FROM checkpoints ORDER BY id DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else {}

    def checkpoint(self, task_id, payload, reason=''):
        payload = dict(payload, agent='omega', task_id=task_id)
        with self.connection() as conn:
            conn.execute('INSERT INTO checkpoints(task_id,created_at,payload,reason) VALUES (?,?,?,?)',
                         (task_id, now(), json.dumps(payload, ensure_ascii=False), reason))
        return payload

    def artifact(self, task_id, name, path=None, content=None):
        with self.connection() as conn:
            conn.execute('INSERT INTO artifacts(task_id,created_at,name,path,content) VALUES (?,?,?,?,?)',
                         (task_id, now(), name, str(path) if path else None, content))

    def enqueue(self, request):
        request_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute('INSERT INTO human_requests(request_id,created_at,updated_at,request) VALUES (?,?,?,?)',
                         (request_id, now(), now(), request))
        return request_id

    def pending_requests(self, limit=10):
        with self.connection(readonly=True) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM human_requests WHERE status IN ('new','delivered') ORDER BY created_at LIMIT ?", (limit,))]

    def mark_delivered(self, request_id):
        with self.connection() as conn:
            conn.execute("UPDATE human_requests SET status='delivered',updated_at=? WHERE request_id=? AND status='new'", (now(), request_id))

    def complete_request(self, request_id, response):
        with self.connection() as conn:
            cursor = conn.execute("UPDATE human_requests SET status='done',response=?,updated_at=? WHERE request_id=?", (response, now(), request_id))
            if not cursor.rowcount:
                raise ValueError('Unknown request_id')

    def settings(self):
        with self.connection(readonly=True) as conn:
            return {row['setting_key']: json.loads(row['value']) for row in conn.execute('SELECT * FROM agent_settings')}

    def set_setting(self, key, value):
        with self.connection() as conn:
            conn.execute('INSERT INTO agent_settings VALUES (?,?,?) ON CONFLICT(setting_key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (key, json.dumps(value), now()))

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise ValueError('Backup destination must be a new file')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.tmp-' + uuid.uuid4().hex)
        try:
            with self.connection(readonly=True) as source:
                target = sqlite3.connect(str(temporary))
                try:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Backup integrity check failed')
                finally:
                    target.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def query(self, sql, parameters=(), max_rows=100, allow_write=False, timeout=10):
        with self.connection(readonly=not allow_write) as conn:
            # Do not let the SQL tool attach other databases or change readonly policy.
            def authorize(action, arg1, arg2, database, trigger):
                if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
                    return sqlite3.SQLITE_DENY
                if not allow_write and action == sqlite3.SQLITE_PRAGMA and arg2 is not None:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            deadline = time.monotonic() + timeout
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            cursor = conn.execute(sql, parameters)
            if cursor.description:
                rows = cursor.fetchmany(max_rows + 1)
                columns = [item[0] for item in cursor.description]
                # Finalize RETURNING cursor before commit, even when truncated.
                result = {'columns': columns, 'rows': [list(row) for row in rows[:max_rows]], 'truncated': len(rows) > max_rows}
                cursor.close()
                return result
            return {'rows_affected': cursor.rowcount}

def get_store():
    # Connections are per operation; a SQLiteStore contains only its path.
    return SQLiteStore()
