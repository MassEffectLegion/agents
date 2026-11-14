"""Bootstrap from files; runtime parameters from agents_main.agent_settings.

Priority: built-in defaults < runtime_defaults.json < longlive.common < longlive.<agent>.
PostgreSQL bootstrap credentials stay in runtime_settings.local.json / PG* environment / pgpass.
DB errors preserve last known good settings; no network requests occur during import.
"""
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
LONGLIVE_AGENT_NAME = 'delta2'
DEFAULT_SETTINGS = {'llama_server_url': 'http://127.0.0.1:8080/v1', 'llama_api_key': 'local', 'agent_slot': 0, 'model_fallback': 'Qwen', 'context_window_fallback': 80000, 'context_usage_percent': 95, 'completion_max_tokens': 8192, 'completion_min_tokens': 1024, 'context_safety_tokens': 512, 'no_tool_response_limit': 2, 'context_warn_percent': 80, 'task_from_db_fallback': False, 'restart_delay_seconds': 5, 'loop_stuck_threshold': 3, 'loop_backoff_base_seconds': 30, 'loop_backoff_cap_seconds': 900, 'loop_escalate_cooldown_seconds': 1800, 'model_timeout_seconds': 600, 'server_timeout_seconds': 10, 'settings_refresh_seconds': 30, 'tool_output_max_chars': 12000, 'file_read_max_chars': 15000, 'sql_max_rows': 500, 'embedding_url': 'http://127.0.0.1:9090', 'embedding_model': 'bge-m3', 'log_level': 'INFO', 'extra_allowed_dirs': [], 'lives_heartbeat_seconds': 120, 'controlled_restart_threshold': 80, 'min_tool_calls_for_controlled_restart': 3, 'controlled_restart_max_per_window': 2, 'controlled_restart_window_seconds': 1800}
_defaults_path = AGENT_ROOT / 'runtime_defaults.json'
_local_path = Path(os.environ.get('AGENT_SETTINGS_FILE', str(AGENT_ROOT / 'runtime_settings.local.json')))
_local = json.loads(_local_path.read_text(encoding='utf-8')) if _local_path.exists() else {}
PG_CONFIG = dict(_local.get('postgres', {}))
for key, env in {'host':'PGHOST', 'port':'PGPORT', 'database':'PGDATABASE', 'user':'PGUSER', 'password':'PGPASSWORD'}.items():
    if env in os.environ:
        PG_CONFIG[key] = os.environ[env]
PG_CONFIG.setdefault('connect_timeout', 10)
PG_CONFIG.setdefault('options', '-c search_path=agents_main,public -c statement_timeout=60000 -c lock_timeout=10000 -c timezone=Europe/Moscow')  # 4868 F1: search_path fallback (rest#21, 23.10)
PG_CONFIG_ASOP = dict(_local.get('asop', {}))

def validate_settings(values):
    values = dict(values)
    ranges = {
        'agent_slot': (0, 10000), 'context_window_fallback': (2048, 10000000),
        'context_usage_percent': (1, 100), 'completion_max_tokens': (1, 1000000),
        'completion_min_tokens': (1, 1000000), 'context_safety_tokens': (0, 1000000),
        'no_tool_response_limit': (1, 100), 'context_warn_percent': (1, 100), 'restart_delay_seconds': (0, 3600),
        'loop_stuck_threshold': (1, 1000), 'loop_backoff_base_seconds': (1, 3600), 'loop_backoff_cap_seconds': (1, 86400), 'loop_escalate_cooldown_seconds': (0, 86400),
        'model_timeout_seconds': (1, 86400), 'server_timeout_seconds': (1, 120),
        'settings_refresh_seconds': (1, 3600), 'tool_output_max_chars': (1000, 1000000),
        'file_read_max_chars': (1, 1000000), 'sql_max_rows': (1, 100000), 'lives_heartbeat_seconds': (10, 3600),
        'controlled_restart_threshold': (50, 94), 'min_tool_calls_for_controlled_restart': (1, 50), 'controlled_restart_max_per_window': (0, 100), 'controlled_restart_window_seconds': (60, 86400),
    }
    decimal_keys = {'context_usage_percent', 'restart_delay_seconds', 'settings_refresh_seconds'}
    for key, (minimum, maximum) in ranges.items():
        number = values[key]
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number):
            raise ValueError(f'{key} must be a finite number')
        if not minimum <= number <= maximum or (key not in decimal_keys and int(number) != number):
            raise ValueError(f'{key} must be between {minimum} and {maximum}')
        if key not in decimal_keys:
            values[key] = int(number)
    if values['completion_min_tokens'] > values['completion_max_tokens']:
        raise ValueError('completion_min_tokens exceeds completion_max_tokens')
    for key in ('llama_server_url', 'embedding_url'):
        if not isinstance(values[key],str) or not values[key].startswith(('http://','https://')):
            raise ValueError(f'{key} must be an HTTP(S) URL')
        values[key] = values[key].rstrip('/')
    for key in ('llama_api_key','model_fallback','embedding_model'):
        if not isinstance(values[key],str) or not values[key]:
            raise ValueError(f'{key} must be a nonempty string')
    if values['log_level'] not in ('DEBUG','INFO','WARNING','ERROR','CRITICAL'):
        raise ValueError('Invalid log_level')
    if not isinstance(values['extra_allowed_dirs'],list) or not all(isinstance(p,str) and os.path.isabs(p) for p in values['extra_allowed_dirs']):
        raise ValueError('extra_allowed_dirs must contain absolute paths')
    return values

def merge_settings(local, common=None, own=None):
    result = dict(DEFAULT_SETTINGS)
    for layer in (local,common,own):
        if layer is None:
            continue
        if not isinstance(layer,dict):
            raise ValueError('Runtime settings must be a JSON object')
        unknown = set(layer) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError('Unknown settings: ' + ', '.join(sorted(unknown)))
        result.update(layer)
    return validate_settings(result)

_file_defaults = json.loads(_defaults_path.read_text(encoding='utf-8')) if _defaults_path.exists() else {}
_settings = merge_settings(_file_defaults)
_last_refresh = None

def get_runtime_settings():
    return dict(_settings)

def load_database_settings(force=False):
    global _settings, _last_refresh
    now = time.monotonic()
    if not force and _last_refresh is not None and now - _last_refresh < _settings['settings_refresh_seconds']:
        return get_runtime_settings()
    _last_refresh = now
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(**PG_CONFIG)
        with conn.cursor() as cur:
            cur.execute("""SELECT setting_key,value FROM agents_main.agent_settings
                WHERE setting_key IN (%s,%s,%s,%s) AND enabled IS NOT FALSE""",
                ('longlive.common','longlive.'+LONGLIVE_AGENT_NAME,'asop_config','bge'))
            rows = dict(cur.fetchall())
        candidate = merge_settings(_file_defaults, rows.get('longlive.common'), rows.get('longlive.'+LONGLIVE_AGENT_NAME))
        bge = rows.get('bge')  # 1b4d0489: ЕДИНАЯ точка BGE (RAG) из agent_settings
        if bge is not None:
            if not isinstance(bge,dict):
                raise ValueError('bge must be a JSON object')
            if isinstance(bge.get('url'),str) and bge['url'].startswith(('http://','https://')):
                candidate['embedding_url'] = bge['url'].rstrip('/')
            if isinstance(bge.get('model'),str) and bge['model']:
                candidate['embedding_model'] = bge['model']
        asop = rows.get('asop_config')
        if asop is not None and not isinstance(asop,dict):
            raise ValueError('asop_config must be a JSON object')
        if candidate != _settings:
            logging.info('Runtime settings refreshed from agent_settings (changed keys: %s)',
                         ', '.join(key for key in candidate if candidate[key] != _settings[key]))
        _settings = candidate
        ALLOWED_DIRS[:] = [str(AGENT_ROOT)] + _extra_dirs + _settings['extra_allowed_dirs']
        if asop is not None:
            PG_CONFIG_ASOP.clear()
            PG_CONFIG_ASOP.update(asop)
    except Exception as exc:
        logging.warning('agent_settings unavailable or invalid; keeping last good/file settings: %s', exc)
    finally:
        if conn is not None:
            conn.close()
    return get_runtime_settings()

_extra_dirs = [p for p in os.environ.get('AGENT_EXTRA_DIRS','').split(os.pathsep) if p]
ALLOWED_DIRS = [str(AGENT_ROOT)] + _extra_dirs + _settings['extra_allowed_dirs']
PYTHON_PATH = sys.executable
TESSERACT_PATH = os.environ.get('TESSERACT_PATH', r'C:\Program Files\Tesseract-OCR\tesseract.exe')
LOG_DIR = str(AGENT_ROOT / 'logs')
LOG_FILE = os.environ.get('AGENT_LOG_FILE', str(Path(LOG_DIR) / 'delta2.txt'))
LONGLIVE_BOOTSTRAP_PROMPT = str(AGENT_ROOT / 'core' / 'bootstrap_prompt.yaml')
# Compatibility names for external imports; runtime consumers use get_runtime_settings().
LLAMA_SERVER_URL = _settings['llama_server_url']
LLAMA_API_KEY = _settings['llama_api_key']
AGENT_SLOT = str(_settings['agent_slot'])
CONTEXT_WINDOW_TOKENS = _settings['context_window_fallback']
COMPLETION_MAX_TOKENS = _settings['completion_max_tokens']
CONTEXT_SAFETY_TOKENS = _settings['context_safety_tokens']
RESTART_DELAY_SECONDS = _settings['restart_delay_seconds']
LOG_LEVEL = _settings['log_level']
