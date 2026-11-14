"""
write_local_file — запись текстового файла (создание/перезапись) + DISPLAY CONTEXT REMAINING.

P1-1 (rev 1.2): prefix-баг путей.
P2-14: os.makedirs не падает при пустом dirname.
"Инструменты на свой каталог своего агента" (человек, b3388012):
ALLOWED_DIRS — из СВОЕГО core/config.py.

CONTEXT DISPLAY (c338c233, human night task 4a530129, rest#25):
  The human asked: "if you exceed 75% context in a tool (e.g. file save), show the
  remaining context to reboot". Every agent action is logged to the messages table
  with token_metrics on the last ASSISTANT message (agent_checkpoint.save_message).
  So this tool queries that and, when context usage >= context_usage_percent
  (default 95, configurable), appends a CONTEXT WARN line to the file-result.

  This is READ-ONLY DB info: it does NOT write to the DB, only reads the metrics
  already logged by main.py. Safe — no main.py changes.
"""
import os
import json
import re

from bravo.core.config import ALLOWED_DIRS, PG_CONFIG
from bravo.core.config import get_runtime_settings

from bravo.tools.path_guard import is_allowed as _is_allowed, resolve_path


def _live_context_window():
    """rest#117 (task 525c01b7) / rest#148 (task b49bfcdf): REAL window of the RUNNING agent.

    Source priority (all local, no network — the llama slot is busy with the active
    generation, HTTP probes from inside a tool would hang ~30s and usually fail):
      1) the LAST 'Context budget: ... window=N ...' line of the agent log
         (main.py logs one per LLM step — the exact window ContextBudget uses right now);
      2) AGENT_ROOT/runtime_context.json (written once per cycle with the discovered
         n_ctx; age relaxed to <24h — it is per-cycle and the slot n_ctx does not
         change mid-session; rest#117's 600s age-check was too strict and made long
         cycles fall back to context_window_fallback => false 80k/90%+ banners);
      3) context_window_fallback (static config).
    Returns (window, source).
    """
    # 1) live log line (most accurate: per LLM step)
    try:
        from bravo.core.config import AGENT_ROOT
        logf = AGENT_ROOT / 'logs' / 'bravo.txt'
        if logf.exists():
            with open(logf, 'rb') as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                chunk = fh.read().decode('utf-8', 'ignore')
            m = None
            for line in chunk.splitlines():
                if 'Context budget:' in line:
                    m = line
            if m:
                wm = re.search(r'window=(\d+)', m)
                if wm and int(wm.group(1)) > 0:
                    return int(wm.group(1)), 'agent log (live)'
    except Exception:
        pass
    # 2) runtime_context.json (per-cycle, relaxed age)
    try:
        from bravo.core.config import AGENT_ROOT
        from datetime import datetime, timezone
        f = AGENT_ROOT / 'runtime_context.json'
        if f.exists():
            data = json.loads(f.read_text(encoding='utf-8'))
            n_ctx = int(data.get('n_ctx') or 0)
            if n_ctx > 0:
                age_ok = True
                ts = data.get('ts')
                if ts:
                    try:
                        age_ok = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() < 86400
                    except (ValueError, TypeError):
                        pass
                if age_ok:
                    return n_ctx, str(data.get('context_source') or 'runtime_context.json')
    except Exception:
        pass
    window = int(get_runtime_settings().get('context_window_fallback', 80000) or 80000)
    return window, 'config fallback'


def _get_context_status(task_id, max_messages=50):
    """Last assistant message's token metrics -> context usage % + remaining.

    rest#117 (task 525c01b7): window is the LIVE server n_ctx (fetch_llama_config),
    not the static context_window_fallback. That fixed false 98% banners when the
    agent ran with a bigger window (e.g. 75997 tokens on a 98048 slot = 77.5%,
    not 98.7% of the 80000 fallback)."""
    try:
        if not task_id:
            return None
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        cur = conn.cursor()
        cur.execute(
            """SELECT token_metrics FROM agents_main.messages
               WHERE task_id=%s AND role='assistant' AND token_metrics IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (task_id,))
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        metrics = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        window, source = _live_context_window()
        last_prompt = int(metrics.get('prompt_tokens', 0) or 0)
        last_completion = int(metrics.get('completion_tokens', 0) or 0)
        usage = ((last_prompt + last_completion) / window * 100) if window else 0.0
        remaining = max(0, window - last_prompt - last_completion)
        pct = int(get_runtime_settings().get('context_usage_percent', 95))
        used_total = last_prompt + last_completion
        policy_limit = int(window * pct / 100.0)  # 85% of the real slot window
        tokens_to_reboot = max(0, policy_limit - used_total)
        return {
            'used_percent': round(usage, 1),
            'remaining_tokens': remaining,
            'remaining_percent': round(max(0.0, 100.0 - usage), 1),
            'approaching_limit': usage >= pct,
            'window': window,
            'window_source': source,
            'policy_limit': policy_limit,
            'tokens_to_reboot': tokens_to_reboot,
        }
    except Exception:
        return None


def write_local_file(file_path: str, content: str) -> str:
    """Create or overwrite a local text file. Auto-creates parent directories.
    Args:
        file_path: Target file path (must be in allowed directories)
        content: Text to write
    """
    try:
        abs_path = resolve_path(file_path)
        if not _is_allowed(abs_path):
            return f"ERROR: Access denied. Allowed dirs: {ALLOWED_DIRS}"

        parent = os.path.dirname(abs_path)
        if parent:  # P2-14: dirname может быть '' для relative-имени
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        result = f"✓ saved: {abs_path}"

        # CONTEXT REMAINING display (c338c233). Read-only: uses metrics main.py logged.
        try:
            task_id = os.environ.get("AGENT_TASK_ID", "").strip()
            status = _get_context_status(task_id)
            if status and status['approaching_limit']:
                flag = "🔴" if status['used_percent'] >= 99 else "⚠️"
                result += (f"\n{flag} CONTEXT {status['used_percent']}% used "
                           f"(window={status['window']} {status.get('window_source','')}) "
                           f"| {status['remaining_tokens']} tok left "
                           f"({status['remaining_percent']}%) "
                           f"| до принуд. рестарта {status.get('tokens_to_reboot', '?')} tok "
                           f"(limit={status.get('policy_limit', '?')})")
        except Exception:
            pass
        return result
    except Exception as e:
        return f"ERROR: {str(e)}"


# Metadata (для registry / docgen)
TOOL_NAME = "write_local_file"
TOOL_ID = "write_local_file"
