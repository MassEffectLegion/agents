"""Bounded asynchronous socket waits with explicit transaction semantics."""
import json
import select
import time
import psycopg2
from psycopg2 import extensions
from langchain_core.tools import tool
from ..core.config import PG_CONFIG, get_runtime_settings
from ..core import tg_readonly_guard as _tgro

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
WRITE_TIMEOUT = 120
SOCKET_GRACE = 5

# Q2-штрих (GO 10.11 16:24, HR a62d6bcc): dual-schema TG permanent fix.
# TG-мост (bravo) работает со схемой public; agents_main - зеркало (рассинхрон outbox).
# search_path=agents_main,public резолвит неквалифицированные TG-имена в зеркало
# -> ответы не уходили в TG (rest#51: MinIO-ответ потерялся). Квалифицируем TG-таблицы в public.
_TG_TABLES = ('tg_message_queue', 'tg_outbox_queue')


def _qualify_tg_tables(query: str) -> str:
    import re
    for _t in _TG_TABLES:
        query = re.sub(r'\b(\w+)\.' + _t + r'\b', 'public.' + _t, query)
        query = re.sub(r'(?<![\w.])' + _t + r'\b', 'public.' + _t, query)
    return query



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
    query = _qualify_tg_tables(query)
    max_rows = max(1, min(int(max_rows), get_runtime_settings()['sql_max_rows']))
    timeout = WRITE_TIMEOUT if allow_write else READ_TIMEOUT
    conn = None
    committing = False
    try:
        settings = dict(PG_CONFIG)
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
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        suffix = ' Commit outcome unknown; verify database state before retrying.' if committing else ''
        return f'PostgreSQL ERROR: {exc}.{suffix}'
    finally:
        if conn is not None:
            # Abort uncommitted work without a blocking rollback/cancel round trip.
            conn.close()
