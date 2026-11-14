"""Bounded asynchronous socket waits with explicit transaction semantics."""
import json
import select
import time
import psycopg2
from psycopg2 import extensions
from langchain_core.tools import tool
from ..core import db_access
from ..core.config import PG_CONFIG, get_runtime_settings
from ..core import tg_readonly_guard as _tgro

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
WRITE_TIMEOUT = 120
SOCKET_GRACE = 5


def _wait(conn, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('PostgreSQL socket deadline exceeded')
        state = conn.poll()
        if state == extensions.POLL_OK:
            return
        if state == extensions.POLL_READ:
            select.select([conn.fileno()], [], [], remaining)
        elif state == extensions.POLL_WRITE:
            select.select([], [conn.fileno()], [], remaining)
        else:
            raise psycopg2.OperationalError(f'Unexpected polling state: {state}')


@tool
def query_postgres_local(query: str, max_rows: int = 100, allow_write: bool = True) -> str:
    """Execute one SQL statement in the local DB. Writes commit with allow_write=True.
    For reads use allow_write=False: a read-only transaction with a 30s statement limit.
    Write mode has a 120s limit. Connect limit is 10s; socket waits have a hard deadline.
    Return JSON columns/rows for SELECT, WITH, RETURNING, EXPLAIN; page reads with LIMIT.
    A network error during COMMIT can mean an unknown outcome: verify before retrying writes.
    """
    if not query.strip():
        return 'ERROR: Empty SQL'
    gerr, allow_write = _tgro.guard_sql(query, allow_write)
    if gerr:
        return gerr
    max_rows = max(1, min(int(max_rows), get_runtime_settings()['sql_max_rows']))
    timeout = WRITE_TIMEOUT if allow_write else READ_TIMEOUT
    conn = None
    committing = False
    try:
        settings = dict(db_access.conn_params())
        settings.update(connect_timeout=CONNECT_TIMEOUT)
        settings['options'] = settings.get('options', '') + f' -c statement_timeout={int(timeout * 1000)}'
        connect_deadline = time.monotonic() + CONNECT_TIMEOUT
        conn = psycopg2.connect(**settings, async_=True)
        _wait(conn, connect_deadline)
        deadline = time.monotonic() + timeout + SOCKET_GRACE
        with conn.cursor() as cursor:
            cursor.execute('BEGIN' if allow_write else 'BEGIN READ ONLY')
            _wait(conn, deadline)
            cursor.execute(query)
            _wait(conn, deadline)
            if cursor.description is not None:
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(max_rows + 1)
                result = {'columns': columns, 'rows': rows[:max_rows], 'truncated': len(rows) > max_rows}
            else:
                result = {'rows_affected': cursor.rowcount}
            committing = True
            cursor.execute('COMMIT')
            _wait(conn, deadline)
            if allow_write:
                db_access.mark_dirty()
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        db_access.on_error(exc)
        suffix = ' Commit outcome unknown; verify database state before retrying.' if committing else ''
        return f'PostgreSQL ERROR: {exc}.{suffix}'
    finally:
        if conn is not None:
            # Abort uncommitted work without a blocking rollback/cancel round trip.
            conn.close()
