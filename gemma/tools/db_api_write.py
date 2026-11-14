# -*- coding: utf-8 -*-
"""gemma db_api_write (rest#29, 02.10): WRITES through gemma db_service API :8888.

WHY: db_service :8888 /query is READ-ONLY by design (403 on writes). The agent's
write-path (checkpoint into analyst_daemon_queue, human_request, task_notes, ...)
currently uses raw psycopg2 (query_postgres_local / bulk_insert_to_postgres). Routing
writes through the agent's OWN API (dogfood) gives: same schema-cache + friendly-error
layer, and visibility of write-tool usage in messages.tool_calls for the human to measure
whether context consumption of the write-path drops.

Write endpoints exposed by db_service:
  * POST /insert   -> generic INSERT ... RETURNING ctid  (table + rows)
  * POST /human_request -> INSERT human_request (agent<->human channel)
For reads use db_api_query (the read-only /query). Fallback if :8888 down = clear error
(agent keeps raw psycopg2 as backup). NON-FATAL.
"""
import json
import os
import urllib.request
import urllib.error
from typing import List, Optional
from langchain_core.tools import tool

API_BASE = os.environ.get('DB_SERVICE_URL', 'http://127.0.0.1:8888')  # d016 S2: env override (LAN 127.0.0.1:8888 when bravo runs elsewhere)
TIMEOUT = 40


def _api_call(method, path, payload=None):
    url = API_BASE + path
    data = json.dumps(payload, default=str).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode('utf-8'))


@tool
def db_api_write(operation: str, table: Optional[str] = None,
                 columns: Optional[List[str]] = None, rows: Optional[List[dict]] = None,
                 agent_task: Optional[str] = None, agent_quest: Optional[str] = None,
                 task_id: Optional[str] = None, status: str = 'wait') -> str:
    """Write via gemma db_service API :8888 (POST /insert or POST /human_request).
    operation='insert': put column names in `columns` and one+ dicts in `rows`; posts to
      /insert, returns {ok, inserted, table, columns} or a friendly error.
    operation='human_request': text in `agent_quest`, agent in `agent_task`,
      task_id in `task_id`, status in ('wait','new','info'); returns {ok, request_id}.
    Reads belong to db_api_query. If the API is down the tool returns UNREACHABLE
    (fallback: query_postgres_local with allow_write=True)."""
    op = (operation or '').strip().lower()
    try:
        if op == 'insert':
            if not table:
                return "ERROR: operation=insert needs table"
            payload = {'table': table, 'rows': rows or []}
            if columns:
                payload['columns'] = columns
            status_code, data = _api_call('POST', '/insert', payload)
            if status_code != 200 or not data.get('ok'):
                return f"API INSERT ERROR: {data.get('error', 'unknown')}"
            return json.dumps({'ok': True, 'via': 'db_api :8888/insert', 'result': data},
                              ensure_ascii=False, default=str)
        if op == 'human_request':
            quest = agent_quest or ''
            if not quest:
                return "ERROR: operation=human_request needs agent_quest"
            payload = {'agent_quest': quest, 'status': status, 'agent_task': agent_task,
                       'task_id': task_id}
            status_code, data = _api_call('POST', '/human_request', payload)
            if status_code != 200 or not data.get('ok'):
                return f"API HR ERROR: {data.get('error', 'unknown')}"
            return json.dumps({'ok': True, 'via': 'db_api :8888/human_request',
                               'request_id': data.get('request_id')},
                              ensure_ascii=False, default=str)
        return f"ERROR: unknown operation '{op}' (use 'insert' or 'human_request')"
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode('utf-8'))
        except Exception:
            body = {}
        return f"API HTTP {e.code}: {body.get('error', str(e))[:300]} (db_service :8888)"
    except Exception as e:
        return f'API UNREACHABLE: {type(e).__name__}: {e} (db_service :8888 down? fallback: query_postgres_local allow_write=True)'


@tool
def db_api_health() -> str:
    """Health + stats of gemma db_service API (DB_SERVICE_URL; gemma: 127.0.0.1:8888 via run_gemma.bat) (GET /health + /self_stats).
    Shows: db ok, total API requests served, requests by route (dogfood visibility)."""
    try:
        _, h = _api_call('GET', '/health')
        _, s = _api_call('GET', '/self_stats')
        return json.dumps({'health': h, 'stats': s}, ensure_ascii=False, default=str)
    except Exception as e:
        return f'API UNREACHABLE: {type(e).__name__}: {e} (db_service :8888 down?)'
