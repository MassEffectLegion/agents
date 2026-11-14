# -*- coding: utf-8 -*-
"""bravo DB service (ed7f28ea, S1): agent<->PostgreSQL middleware on 127.0.0.1:8888.

Design (plan/ed7f28ea_analysis_bravo.md):
  * Flask + psycopg2 (fastapi/uvicorn not installed in this env).
  * READ-ONLY by default. Writes only via explicit, validated endpoints
    (/human_request, /human_task_queue/claim, /mailbox, /plans).
  * Schema cache (TTL 30 min) -> agents stop calling get_table_schema.
  * One-line actionable errors: 'column X missing in table Y; columns: [...]'.
  * Auto-LIMIT on reads (default 200, max 2000). No 'pull everything'.
  * ensure_db_service(): idempotent + NON-FATAL (agent falls back to raw SQL).
  * CLI:  python -m bravo.core.db_service [--selftest] [--foreground]
"""
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request

AGENT = os.environ.get('DB_SERVICE_AGENT', 'bravo')
HOST = os.environ.get('DB_SERVICE_HOST', '0.0.0.0')  # d016 S1: LAN access via 127.0.0.1:8888
DEFAULT_PORT = 8888
SCHEMA_TTL = 1800  # 30 min
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
WORKDIR = Path(r'<AGENTS_DIR>\workdir')
INBOX_DIRS = WORKDIR / 'sharedirs'
LOG_FILE = Path(__file__).resolve().parents[1] / 'logs' / 'db_service.log'

# ---------------- logging ----------------
def _log():
    lg = logging.getLogger('bravo.db_service')
    if not lg.handlers:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            h = logging.FileHandler(str(LOG_FILE), encoding='utf-8')
            h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            lg.addHandler(h)
            lg.addHandler(logging.StreamHandler())
            lg.setLevel(logging.INFO)
        except Exception:
            lg.addHandler(logging.NullHandler())
    return lg

# ---------------- DB layer ----------------
_tls = threading.local()

def get_pg_config():
    # Lazy import: core.config pulls its own settings; keep this module standalone-importable.
    try:
        from bravo.core.config import PG_CONFIG
        return dict(PG_CONFIG)
    except Exception:
        p = Path(__file__).resolve().parents[1] / 'runtime_settings.local.json'
        if p.exists():
            return dict(json.loads(p.read_text(encoding='utf-8')).get('postgres', {}))
        return {'host': '127.0.0.1', 'port': 5432, 'database': 'agents',
                'user': 'agents', "password": "CHANGE_ME", 'connect_timeout': 10}

_conn_lock = threading.Lock()

def _connect():
    import psycopg2
    cfg = get_pg_config()
    cfg.setdefault('connect_timeout', 10)
    return psycopg2.connect(**cfg)

def db_conn():
    """Thread-local connection with liveness re-check."""
    c = getattr(_tls, 'conn', None)
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute('SELECT 1')
                cur.fetchone()
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _tls.conn = None
    with _conn_lock:
        c = _connect()
        _tls.conn = c
        return c

def db_read(sql, params=None):
    c = db_conn()
    with c.cursor() as cur:
        # rest#168 fix: params=None -> execute(sql) БЕЗ client-side % formatting
        # (старое `params or ()` ломало LIKE '%x%': psycopg2 делал query%() -> краш).
        cur.execute(sql, params) if params else cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    return cols, rows

def db_write(sql, params=None, commit=True):
    c = db_conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            out = None
            if cur.description:
                out = ([d[0] for d in cur.description], cur.fetchall())
            if commit:
                c.commit()
            return out
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise

# ---------------- JSON-safe encoder ----------------
def _enc(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (uuid.UUID,)):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode('utf-8', 'replace')
    if isinstance(o, memoryview):
        return bytes(o).decode('utf-8', 'replace')
    return str(o)

def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, default=_enc)

# ---------------- schema cache ----------------
_schema_cache = {}  # 'schema.table' -> (ts, {table: [cols...]})
_schema_lock = threading.Lock()

def table_columns(table):
    """Return {table_name: [column names]} for given table (may be schema.table)."""
    t = table.strip()
    m = re.search(r'([a-z_][\w]*)\.[a-z_][\w]*$', t)
    schema = (m.group(1) if m else 'public')
    base = t.split('.')[-1]
    key = f'{schema}.{base}'
    now = time.time()
    with _schema_lock:
        hit = _schema_cache.get(key)
        if hit and now - hit[0] < SCHEMA_TTL:
            return hit[1].get(base)
    cols = None
    try:
        _, rows = db_read(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
            (schema, base))
        cols = [r[0] for r in rows]
    except Exception:
        cols = None
    if cols is not None:
        with _schema_lock:
            hit = _schema_cache.get(key) or (now, {})
            hit[1][base] = cols
            _schema_cache[key] = (now, hit[1])
    return cols

def parse_tables_from_sql(sql):
    """Extract candidate table names from SQL (FROM/JOIN/INTO/UPDATE)."""
    names = []
    for m in re.finditer(r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+([a-zA-Z_][\w.]*)', sql, re.I):
        t = m.group(1)
        if t.upper() not in ('SELECT', 'ONLY', 'LATERAL'):
            names.append(t)
    return names

def friendly_error(exc):
    try:
        return _friendly_error_impl(exc)
    except Exception as e:
        try:
            return 'ERROR: ' + str(e)[:200]
        except Exception:
            return 'ERROR: (unprintable exception)'


def _friendly_error_impl(exc):
    """Turn a psycopg2 error (RU or EN message) into ONE actionable line.

    Cases:
      RU:  ОШИБКА: столбец "x" не существует          -> column missing + columns list
      RU:  ОШИБКА: отношение "t" не существует        -> table missing + close tables
      RU:  ОШИБКА: синтаксическая ошибка ...          -> syntax, first line
      EN:  column "x" does not exist / relation "t" does not exist / syntax error
      else: first line, truncated.
    """
    msg = str(exc) or str(type(exc).__name__)
    first = msg.splitlines()[0] if msg else type(exc).__name__
    # psycopg2: no .query attr — the SQL is embedded in str(exc) after 'LINE 1:'.
    sql = getattr(exc, 'query', None) or ''
    if not sql:
        mline = re.search(r'LINE 1:\s*(.+)', msg)
        if mline:
            sql = mline.group(1)
    tables = parse_tables_from_sql(sql)

    m = re.search(r'столбец\s+"([^"]+)"\s+не существует|column\s+"([^"]+)"\s+does not exist', first, re.I)
    if m:
        col = m.group(1) or m.group(2)
        hint = ''
        guessed = ''
        for t in tables:
            cols = table_columns(t)
            if cols:
                guessed = t
                hint = f' columns in {t}: {", ".join(cols[:40])}'
                break
        return f'column "{col}" not found (table guess: {guessed or (tables[0] if tables else "?")});{hint}'

    m = re.search(r'отношение\s+"([^"]+)"\s+не существует|relation\s+"([^"]+)"\s+does not exist', first, re.I)
    if m:
        rel = m.group(1) or m.group(2)
        hint = ''
        try:
            base = rel.split('.')[-1]
            _, rows = db_read(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name LIKE %s LIMIT 5", (base + '%',))
            if rows:
                hint = ' close tables: ' + ', '.join(r[0] for r in rows)
        except Exception:
            pass
        return f'table "{rel}" not found;{hint}'

    if 'синтаксическая ошибка' in first or 'syntax error' in first.lower():
        return 'SQL syntax error; ' + first[:220]

    return first[:300]

# ---------------- app ----------------
app = Flask(__name__)
STATS = {'started_at': datetime.utcnow().isoformat(), 'requests': 0, 'errors': 0,
         'by_route': {}, 'est_tokens_total': 0}

def _route_rec(route):
    rec = STATS['by_route'].get(route)
    if not isinstance(rec, dict):
        rec = {'calls': int(rec) if isinstance(rec, int) else 0, 'est_tokens': 0, 'last_ts': None}
        STATS['by_route'][route] = rec
    return rec

def _est_tokens(payload):
    return int((len(json.dumps(payload, ensure_ascii=False, default=_enc)) + 3) // 3.5)

def _is_cached(table):
    t = table.strip()
    m = re.search(r'([a-z_][\w]*)\.[a-z_][\w]*$', t)
    schema = (m.group(1) if m else 'public')
    base = t.split('.')[-1]
    hit = _schema_cache.get(f'{schema}.{base}')
    return bool(hit and time.time() - hit[0] < SCHEMA_TTL)

@app.after_request
def _track(resp):
    STATS['requests'] += 1
    route = request.path
    rec = _route_rec(route)
    rec['calls'] += 1
    rec['last_ts'] = datetime.utcnow().isoformat()
    return resp

@app.route('/health')
def health():
    ok, detail = True, 'up'
    try:
        db_read('SELECT 1')
    except Exception as e:
        ok, detail = False, str(e)[:200]
    return jsonify({'ok': ok, 'db': detail, 'agent': AGENT, 'port': request.environ.get('SERVER_PORT')})

@app.route('/tables')
def list_tables():
    try:
        _, rows = db_read(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        return jsonify({'tables': [r[0] for r in rows]})
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/schema')
def get_schema():
    tables = (request.args.get('tables') or '').split(',')
    out = {}
    cached_all = True
    for t in tables:
        t = t.strip()
        if not t:
            continue
        if not _is_cached(t):
            cached_all = False
        cols = table_columns(t)
        out[t] = cols  # None if unknown
    cache_state = 'cached' if (out and cached_all) else 'refreshed'
    payload = {'schema': out, 'ttl_sec': SCHEMA_TTL, 'cache': cache_state}
    tok = _est_tokens(payload) if cache_state == 'refreshed' else 0
    payload['est_tokens'] = tok
    STATS['est_tokens_total'] += tok
    _route_rec(request.path)['est_tokens'] += tok
    return jsonify(payload)

@app.route('/query', methods=['POST'])
def do_query():
    body = request.get_json(silent=True) or {}
    sql = (body.get('sql') or '').strip()
    if not sql:
        return jsonify({'error': 'empty sql'}), 400
    limit = int(body.get('limit') or DEFAULT_LIMIT)
    limit = max(1, min(limit, MAX_LIMIT))
    head = sql.lstrip().upper()
    is_read = head.startswith(('SELECT', 'WITH', 'EXPLAIN'))
    if not is_read:
        return jsonify({'error': 'read-only: only SELECT/WITH/EXPLAIN allowed in /query; '
                                 'use explicit write endpoints'}), 403
    if not sql.rstrip().endswith(';'):
        sql += ';'
    has_limit = bool(re.search(r'\bLIMIT\s+\d+', sql, re.I))
    if not has_limit and limit < MAX_LIMIT:
        sql = sql.rstrip().rstrip(';') + f' LIMIT {limit};'
    t0 = time.time()
    try:
        cols, rows = db_read(sql)
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 400
    rows = rows[:limit]
    payload = {'ok': True, 'columns': cols, 'rows': [[_enc(v) for v in r] for r in rows],
               'count': len(rows), 'ms': round((time.time() - t0) * 1000, 1)}
    tok = _est_tokens(payload)
    payload['est_tokens'] = tok
    STATS['est_tokens_total'] += tok
    _route_rec(request.path)['est_tokens'] += tok
    return jsonify(payload)

@app.route('/insert', methods=['POST'])
def do_insert():
    body = request.get_json(silent=True) or {}
    table = (body.get('table') or '').strip()
    rows = body.get('rows') or []
    if not re.fullmatch(r'[a-z_][\w]*(\.[a-z_][\w]*)?', table):
        return jsonify({'error': 'invalid table name'}), 400
    if not rows or not isinstance(rows, list):
        return jsonify({'error': 'rows must be non-empty list of objects'}), 400
    known = table_columns(table)
    if known is None:
        return jsonify({'error': f'table {table} not found (schema lookup failed)'}), 400
    all_cols = set()
    for r in rows:
        all_cols |= set(r.keys())
    missing = sorted(c for c in all_cols if c not in known)
    if missing:
        return jsonify({'error': f'columns missing in {table}: {missing}; available: {known[:40]}'}), 400
    cols = sorted(all_cols)
    # rest#253 fix: one placeholder set PER ROW (was single set for N rows ->
    # psycopg2 'not all arguments converted' on 2+ rows).
    row_ph = '(' + ','.join(['%s'] * len(cols)) + ')'
    sql = (f'INSERT INTO {table} ({",".join(cols)}) '
           f'VALUES {",".join([row_ph] * len(rows))} RETURNING ctid')
    if 'id' in known:
        sql = sql.replace('RETURNING ctid', 'RETURNING id')
    params = []
    for r in rows:
        for c in cols:
            v = r.get(c)
            if isinstance(v, (dict, list)):
                # c168 fix: jsonb/json columns need a JSON string, not a python dict
                # (psycopg2 'can't adapt type dict'; mirrors /update serialization).
                v = json.dumps(v, ensure_ascii=False, default=_enc)
            params.append(v)
    try:
        out = db_write(sql, params, commit=True)
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 400
    ids = [row[0] for row in out[1]] if out and out[1] else None
    return jsonify({'ok': True, 'inserted': len(rows), 'table': table, 'columns': cols,
                    'ids': ids})

# ---------------- /update (rest#42, e6cf3efa: LIMA write-канал) ----------------
# Spec человека (msg77, 07.10): запись в БД ТОЛЬКО через инструмент, POST /update,
# СТРОГО operator_task_queue. Allowlist — единственная дверь; остальное 403.
UPDATE_ALLOWED_TABLES = {'operator_task_queue'}

def _jsonb_columns(table):
    t = table.strip()
    m = re.search(r'([a-z_][\w]*)\.[a-z_][\w]*$', t)
    schema = (m.group(1) if m else 'public')
    base = t.split('.')[-1]
    try:
        _, rows = db_read(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND data_type IN ('jsonb','json')",
            (schema, base))
        return [r[0] for r in rows]
    except Exception:
        return []

@app.route('/update', methods=['POST'])
def do_update():
    body = request.get_json(silent=True) or {}
    table = (body.get('table') or '').strip()
    if table not in UPDATE_ALLOWED_TABLES:
        return jsonify({'error': f'update not allowed for table {table or "(none)"}; '
                                 f'allowed: {sorted(UPDATE_ALLOWED_TABLES)}'}), 403
    where = body.get('where') or {}
    setv = body.get('set') or {}
    if not isinstance(where, dict) or not where:
        return jsonify({'error': 'where must be a non-empty {col: value} object'}), 400
    if not isinstance(setv, dict) or not setv:
        return jsonify({'error': 'set must be a non-empty {col: value} object'}), 400
    known = table_columns(table)
    if known is None:
        return jsonify({'error': f'table {table} not found (schema lookup failed)'}), 400
    missing = sorted(c for c in (set(where) | set(setv)) if c not in known)
    if missing:
        return jsonify({'error': f'columns missing in {table}: {missing}; available: {known[:40]}'}), 400
    where_sql = ' AND '.join([f'{c}=%s' for c in sorted(where)])
    wparams = [where[c] for c in sorted(where)]
    set_cols = sorted(setv)
    if 'updated_at' in known and 'updated_at' not in setv:
        set_cols = set_cols + ['updated_at']
    set_parts, sparams = [], []
    for c in set_cols:
        if c == 'updated_at':
            set_parts.append('updated_at=NOW()')
            continue
        v = setv[c]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, default=_enc)
        sparams.append(v)
        set_parts.append(f'{c}=%s')
    ret = 'RETURNING id' if 'id' in known else ''
    sql = f'UPDATE {table} SET {", ".join(set_parts)} WHERE {where_sql} {ret}'
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, sparams + wparams)
            n = cur.rowcount
            ids = [r[0] for r in cur.fetchall()] if cur.description else None
            conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 400
    return jsonify({'ok': True, 'updated': n, 'table': table, 'ids': ids,
                    'where_cols': sorted(where), 'set_cols': sorted(setv)})

@app.route('/human_request')
def hr_list():
    agent = request.args.get('agent', AGENT)
    statuses = (request.args.get('status') or 'wait,new').split(',')
    limit = min(int(request.args.get('limit', 10)), 50)
    ph = ','.join(['%s'] * len(statuses))
    try:
        _, rows = db_read(
            f"SELECT request_id, status, left(agent_quest, 400), left(human_response, 400), created_at "
            f"FROM human_request WHERE agent_task=%s AND status IN ({ph}) "
            f"ORDER BY created_at DESC LIMIT %s", [agent, *statuses, limit])
        return jsonify({'items': [{'id': r[0], 'status': r[1], 'quest': r[2],
                                   'human_response': r[3], 'created_at': _enc(r[4])} for r in rows]})
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/human_request', methods=['POST'])
def hr_write():
    body = request.get_json(silent=True) or {}
    quest = (body.get('agent_quest') or body.get('quest') or '').strip()
    if not quest:
        return jsonify({'error': 'agent_quest (or quest) required'}), 400
    agent_task = body.get('agent_task') or AGENT
    task_id = body.get('task_id') or None
    status = body.get('status') or 'wait'
    # 15a70d80 follow-up (29.10, bravo): align with DB check constraint
    # human_request_status_check (wait/done/replied/archived); old list
    # (wait/new/info) rejected by DB on insert.
    if status not in ('wait', 'done', 'replied', 'archived'):
        return jsonify({'error': "status must be wait/done/replied/archived"}), 400
    rid = str(uuid.uuid4())
    try:
        db_write("INSERT INTO human_request (request_id, task_id, agent_task, agent_quest, status) "
                 "VALUES (%s, %s, %s, %s, %s)", (rid, task_id, agent_task, quest, status), commit=True)
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 400
    return jsonify({'ok': True, 'request_id': rid, 'status': status})

@app.route('/human_response')
def hr_response():
    rid = request.args.get('id')
    if not rid:
        return jsonify({'error': 'id required'}), 400
    try:
        _, rows = db_read("SELECT status, human_response, finish_at FROM human_request "
                          "WHERE request_id=%s", (rid,))
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500
    if not rows:
        return jsonify({'found': False}), 404
    r = rows[0]
    return jsonify({'found': True, 'status': r[0], 'human_response': r[1], 'finish_at': _enc(r[2])})

@app.route('/human_task_queue')
def htq_list():
    statuses = (request.args.get('status') or 'new,in_progress').split(',')
    ph = ','.join(['%s'] * len(statuses))
    try:
        _, rows = db_read(
            f"SELECT task_id, status, claimed_by, task_notes->>'pipeline', left(task, 200), created_at "
            f"FROM human_task_queue WHERE status IN ({ph}) ORDER BY created_at DESC LIMIT 20",
            statuses)
        return jsonify({'items': [{'id': r[0], 'status': r[1], 'owner': r[2], 'pipeline': r[3],
                                   'task': r[4], 'created_at': _enc(r[5])} for r in rows]})
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/human_task_queue/claim', methods=['POST'])
def htq_claim():
    body = request.get_json(silent=True) or {}
    tid = (body.get('task_id') or '').strip()
    agent = (body.get('agent') or AGENT).strip()
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', tid):
        return jsonify({'error': 'task_id must be uuid'}), 400
    try:
        out = db_write(
            "UPDATE human_task_queue SET claimed_by=%s, status='in_progress', assigned_at=NOW(), "
            "task_notes=(COALESCE(task_notes,'{}'::jsonb) || "
            "jsonb_build_object('readers', COALESCE(task_notes->'readers','{}'::jsonb) || "
            "jsonb_build_object(%s, NOW()::text))) "
            "WHERE task_id=%s AND claimed_by IS NULL AND status IN ('new','open') "
            "RETURNING task_id", (agent, agent, tid), commit=True)
        got = bool(out and out[1])
        if got:
            db_write("INSERT INTO agent_coordination (topic, action, payload, agent) VALUES (%s,%s,%s,%s)",
                     (f'task:{tid}', 'claim', json.dumps({'agent': agent, 'ts': datetime.utcnow().isoformat()}), agent),
                     commit=True)
        return jsonify({'claimed': got, 'agent': agent})
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/human_task_queue/notes')
def htq_notes():
    tid = request.args.get('id')
    try:
        _, rows = db_read("SELECT task_notes FROM human_task_queue WHERE task_id=%s", (tid,))
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500
    return jsonify({'notes': rows[0][0] if rows else None})

@app.route('/mailbox', methods=['POST'])
def mailbox_write():
    body = request.get_json(silent=True) or {}
    to = (body.get('to') or '').strip()
    frm = (body.get('from') or AGENT).strip()
    text = (body.get('text') or '').strip()
    if to not in ('alfa', 'bravo', 'omega') or not text:
        return jsonify({'error': 'to in (alfa,bravo,omega) + text required'}), 400
    try:
        d = INBOX_DIRS / f'inbox_{to}'
        d.mkdir(parents=True, exist_ok=True)
        fn = d / f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_{frm}.md'
        fn.write_text(f'FROM: {frm}\nTO: {to}\nTS: {datetime.utcnow().isoformat()}Z\n\n{text}\n',
                      encoding='utf-8')
        return jsonify({'ok': True, 'file': str(fn)})
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/plans')
def plans_read():
    task8 = request.args.get('task', '').strip()[:8]
    d = WORKDIR / 'plan'
    if not task8:
        try:
            names = sorted(p.name for p in d.glob('*.md'))
            return jsonify({'files': names[-80:]})
        except Exception as e:
            return jsonify({'error': friendly_error(e)}), 500
    try:
        matches = sorted(d.glob(f'{task8}_*.md'))
        out = []
        for p in matches:
            head = p.read_text(encoding='utf-8', errors='replace')[:1500]
            out.append({'file': str(p), 'size': p.stat().st_size, 'head': head})
        return jsonify({'files': out})
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/plans', methods=['POST'])
def plans_write():
    body = request.get_json(silent=True) or {}
    task8 = (body.get('task') or '').strip()[:8]
    name = re.sub(r'[^A-Za-z0-9_]', '_', (body.get('name') or 'note'))[:40]
    content = body.get('content')
    if not task8 or not re.fullmatch(r'[A-Za-z0-9_]+', name) or not isinstance(content, str):
        return jsonify({'error': 'task(8)+name+content(str) required'}), 400
    if name.lower() in ('..', '..md', 'xss'):
        return jsonify({'error': 'bad name'}), 400
    try:
        d = WORKDIR / 'plan'
        d.mkdir(parents=True, exist_ok=True)
        fn = d / f'{task8}_{name}.md'
        fn.write_text(content, encoding='utf-8')
        return jsonify({'ok': True, 'file': str(fn), 'bytes': len(content.encode('utf-8'))})
    except Exception as e:
        STATS['errors'] += 1
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/self_stats')
def self_stats():
    return jsonify(STATS)

@app.route('/tool_metrics')
def tool_metrics():
    return jsonify(STATS)

@app.route('/context_usage')
def context_usage():
    agent = (request.args.get('agent') or AGENT).strip() or None
    lim = min(int(request.args.get('limit', 20)), 100)
    if agent:
        sql = ("SELECT timestamp, role, left(content, 120), tool_calls, token_metrics "
               "FROM messages WHERE agent=%s AND tool_calls IS NOT NULL AND tool_calls <> '' "
               "ORDER BY timestamp DESC LIMIT %s")
        params = [agent, lim]
    else:
        sql = ("SELECT timestamp, role, left(content, 120), tool_calls, token_metrics "
               "FROM messages WHERE tool_calls IS NOT NULL AND tool_calls <> '' "
               "ORDER BY timestamp DESC LIMIT %s")
        params = [lim]
    try:
        _, rows = db_read(sql, params)
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500
    calls = []
    total_prompt = 0
    for ts, role, content, tcs, tm in rows:
        names = []
        try:
            names = [c.get('name') for c in json.loads(tcs) if isinstance(c, dict)]
        except Exception:
            pass
        p = None
        try:
            p = json.loads(tm).get('prompt_tokens') if tm else None
        except Exception:
            pass
        if isinstance(p, int):
            total_prompt += p
        calls.append({'ts': _enc(ts), 'role': role, 'head': content,
                      'tools': names, 'prompt_tokens': p})
    return jsonify({'agent': agent, 'calls': calls,
                    'last_prompt_tokens': calls[0]['prompt_tokens'] if calls else None,
                    'sum_prompt_tokens_last_n': total_prompt})

# ---------------- runner / selftest ----------------
def port_free(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return False
    except OSError:
        return True

def is_our_service(host, port):
    """Best-effort: is the service already running (our health check)?"""
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://{host}:{port}/health', timeout=2) as r:
            data = json.loads(r.read().decode('utf-8'))
            return bool(data.get('ok'))
    except Exception:
        return False

def ensure_db_service(agent=AGENT, port=DEFAULT_PORT):
    """Idempotent + NON-FATAL: start the service if not running. Returns dict."""
    lg = _log()
    if is_our_service('127.0.0.1', port):  # d016 S1: check loopback; bind host may be 0.0.0.0
        return {'status': 'already_running', 'port': port}
    if not port_free('127.0.0.1', port):
        lg.warning('db_service: port %s busy by non-service process; skipping (raw SQL fallback)', port)
        return {'status': 'port_busy', 'port': port}
    try:
        proc = subprocess.Popen(
            [sys.executable, '-u', str(Path(__file__).resolve()), '--foreground'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    except Exception as e:
        lg.warning('db_service start failed: %s', e)
        return {'status': 'failed', 'error': str(e)[:200]}
    ok = False
    for _ in range(20):
        time.sleep(0.5)
        if is_our_service(HOST, port):
            ok = True
            break
    if ok:
        lg.info('db_service started on %s:%s pid=%s', HOST, port, proc.pid)
        return {'status': 'started', 'port': port, 'pid': proc.pid}
    lg.warning('db_service did not come up in 10s (proc pid=%s); raw SQL fallback', proc.pid)
    return {'status': 'not_up', 'port': port, 'pid': proc.pid}

def run_selftest(port=DEFAULT_PORT):
    """Exercise every endpoint against the live DB (read-only + 1 throwaway mailbox/plan file)."""
    import urllib.request
    base = f'http://127.0.0.1:{port}'  # d016 S1: test via loopback; HOST may be 0.0.0.0 (bind-only)
    results = {}
    def call(method, path, body=None, expect_ok=True):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))
    s, d = call('GET', '/health'); results['health'] = (s, d)
    s, d = call('GET', '/tables'); results['tables'] = (s, len(d.get('tables', [])))
    s, d = call('GET', '/schema?tables=human_request,human_task_queue')
    results['schema'] = (s, {k: (len(v) if v else None) for k, v in (d.get('schema') or {}).items()})
    s, d = call('POST', '/query', {'sql': "SELECT count(*) AS n FROM human_request", 'limit': 1})
    results['query'] = (s, d)
    s, d = call('POST', '/query', {'sql': 'SELECT * FROM nonexistent_table_xyz', 'limit': 1})
    results['query_missing_table'] = (s, d)
    s, d = call('POST', '/query', {'sql': 'SELECT bogus_col FROM human_request', 'limit': 1})
    results['query_missing_col'] = (s, d)
    s, d = call('POST', '/query', {'sql': "DELETE FROM human_request", 'limit': 1})
    results['query_write_blocked'] = (s, d)
    s, d = call('POST', '/insert', {'table': 'human_request',
                                    'rows': [{'no_such_column': 1}]})
    results['insert_bad_col'] = (s, d)
    s, d = call('POST', '/update', {'table': 'operator_task_queue',
                                    'where': {'id': '00000000-0000-0000-0000-000000000000'},
                                    'set': {'status': 'selftest'}})
    results['update_no_match'] = (s, d)
    s, d = call('POST', '/update', {'table': 'human_request',
                                    'where': {'request_id': 'x'}, 'set': {'status': 'x'}})
    results['update_not_allowed'] = (s, d)
    s, d = call('GET', '/human_request?agent=bravo&status=wait,new&limit=3')
    results['hr_list'] = (s, len(d.get('items', [])))
    s, d = call('GET', '/human_task_queue?status=new,in_progress')
    results['htq_list'] = (s, len(d.get('items', [])))
    s, d = call('GET', '/human_task_queue/notes?id=ed7f28ea-70eb-43da-8c24-d6c59e9ec6e6')
    results['htq_notes'] = (s, bool(d.get('notes')))
    s, d = call('POST', '/mailbox', {'to': 'alfa', 'from': 'bravo',
                                     'text': f'[selftest {datetime.utcnow().isoformat()}Z] db_service selftest ping — игнорируйте.'})
    results['mailbox'] = (s, d.get('file', d))
    s, d = call('POST', '/plans', {'task': 'ed7f28ea', 'name': 'selftest_ping',
                                   'content': f'# selftest ping {datetime.utcnow().isoformat()}Z\n'})
    results['plans_write'] = (s, d.get('file', d))
    s, d = call('GET', '/plans?task=ed7f28ea')
    results['plans_read'] = (s, len(d.get('files', [])))
    s, d = call('GET', '/self_stats')
    results['self_stats'] = (s, {k: d.get(k) for k in ('requests', 'errors')})
    s, d = call('GET', '/tool_metrics')
    results['tool_metrics'] = (s, bool(d.get('by_route')))
    s, d = call('GET', '/context_usage?agent=bravo&limit=5')
    results['context_usage'] = (s, len(d.get('calls', [])))
    return results

def main(argv):
    lg = _log()
    port = DEFAULT_PORT
    if '--port' in argv:
        port = int(argv[argv.index('--port') + 1])
    if '--selftest' in argv:
        res = run_selftest(port)
        print(json.dumps(res, indent=2, ensure_ascii=False, default=_enc))
        return 0
    if '--foreground' in argv:
        if is_our_service('127.0.0.1', port):
            lg.info('db_service --foreground: service already healthy on 127.0.0.1:%s; exiting (dedup, d016 S1)', port)
            return 0
        app.run(host=HOST, port=port, threaded=True)
        return 0
    res = ensure_db_service(AGENT, port)
    print(json.dumps(res, default=_enc))
    return 0 if res.get('status') in ('started', 'already_running') else 1

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
