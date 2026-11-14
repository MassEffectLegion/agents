# -*- coding: utf-8 -*-
"""bravo db_api_query (c39a0bfa, 01.10): READS through bravo db_service API 127.0.0.1:8888.

WHY THIS TOOL EXISTS (human order c39a0bfa 01.10 01:13 MSK):
  * dogfood: the agent must USE its own :8888 API for DB reads (not raw psycopg2);
  * the table `messages.tool_calls` must show tool name 'db_api_query' so the human
    can see (1-2 days) that the tool IS the API interaction and measure whether
    context consumption improves vs query_postgres_local.
  * read-only by design: only SELECT/WITH/EXPLAIN (server-side guard too, 403 otherwise).

Fallback: if :8888 is down, the tool returns a clear one-line error (the agent keeps
raw-SQL tool query_postgres_local as backup). NON-FATAL by design.
"""
import json
import os
import urllib.request
import urllib.error
from langchain_core.tools import tool

API_BASE = os.environ.get('DB_SERVICE_URL', 'http://127.0.0.1:8888')  # d016 S2: env override (LAN 127.0.0.1:8888 when bravo runs elsewhere)
TIMEOUT = 40


def _api_call(method, path, payload=None):
    url = API_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode('utf-8'))


@tool
def db_api_query(query: str, max_rows: int = 100) -> str:
    """Read-only SQL via bravo db_service API (127.0.0.1:8888 POST /query).
    ONLY SELECT/WITH/EXPLAIN (server enforces read-only). Returns JSON
    {columns, rows, count, ms} — ms = API round-trip time. This tool IS the
    API interaction (dogfood, c39a0bfa); visible in messages.tool_calls.
    For writes use dedicated tools (query_postgres_local or service write endpoints).
    """
    q = (query or '').strip()
    if not q:
        return 'ERROR: empty SQL'
    try:
        status, data = _api_call('POST', '/query', {'sql': q, 'limit': max(1, min(int(max_rows), 2000))})
        if status != 200 or not data.get('ok'):
            return f"API ERROR: {data.get('error', 'unknown')}"
        return json.dumps({'columns': data.get('columns'), 'rows': data.get('rows'),
                           'count': data.get('count'), 'ms': data.get('ms'), 'via': 'db_api :8888'},
                          ensure_ascii=False, default=str)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode('utf-8'))
        except Exception:
            body = {}
        return f"API HTTP {e.code}: {body.get('error', str(e))[:300]}"
    except Exception as e:
        return f'API UNREACHABLE: {type(e).__name__}: {e} (db_service :8888 down? fallback: query_postgres_local)'


@tool
def db_api_health() -> str:
    """Health + stats of bravo db_service API 127.0.0.1:8888 (GET /health + /self_stats).
    Shows: db ok, total API requests served, requests by route (dogfood visibility)."""
    try:
        _, h = _api_call('GET', '/health')
        _, s = _api_call('GET', '/self_stats')
        return json.dumps({'health': h, 'stats': s}, ensure_ascii=False, default=str)
    except Exception as e:
        return f'API UNREACHABLE: {type(e).__name__}: {e} (db_service :8888 down?)'
