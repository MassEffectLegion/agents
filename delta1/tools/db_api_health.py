# -*- coding: utf-8 -*-
"""bravo db_api_health (c5db9f76, 01.10, fix by alfa rest#14): health endpoint tool.

CRASH CAUSE (human report c5db9f76): core/tools_registry.py imports
`from bravo.tools.db_api_health import db_api_health` — a SEPARATE module —
but the function was defined inside tools/db_api_query.py. ModuleNotFoundError
killed the agent at startup (get_tools -> import_module).

FIX: this standalone module. Thin wrapper over the shared _api_call from
db_api_query (no code duplication; the /health + /self_stats logic is the same
single API call the query tool uses, so both tools hit the same :8888 service).
"""
import json
import urllib.error
from langchain_core.tools import tool

from bravo.tools.db_api_query import _api_call


@tool
def db_api_health() -> str:
    """Health + stats of bravo db_service API 127.0.0.1:8888 (GET /health + /self_stats).
    Shows: db ok, total API requests served, requests by route (dogfood visibility)."""
    try:
        _, h = _api_call('GET', '/health')
        _, s = _api_call('GET', '/self_stats')
        return json.dumps({'health': h, 'stats': s}, ensure_ascii=False, default=str)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode('utf-8'))
        except Exception:
            body = {}
        return f"API HTTP {e.code}: {body.get('error', str(e))[:300]} (db_service :8888)"
    except Exception as e:
        return f'API UNREACHABLE: {type(e).__name__}: {e} (db_service :8888 down?)'
