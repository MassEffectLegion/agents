"""File bootstrap and optional SQLite runtime overrides. Never contacts PostgreSQL."""
import copy
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get('OMEGA_CONFIG', str(AGENT_ROOT / 'config.json'))).resolve()
LOG_DIR = str(AGENT_ROOT / 'logs')
LOG_FILE = os.environ.get('AGENT_LOG_FILE', str(Path(LOG_DIR) / 'omega.log'))
LONGLIVE_AGENT_NAME = 'omega'

RANGES = {
    'context_usage_percent': (1, 100), 'completion_max_tokens': (1, 1000000),
    'completion_min_tokens': (1, 1000000), 'context_safety_tokens': (0, 1000000),
    'no_tool_response_limit': (1, 100), 'restart_delay_seconds': (0, 3600),
    'settings_refresh_seconds': (1, 3600), 'model_timeout_seconds': (1, 86400),
    'server_timeout_seconds': (1, 120), 'tool_output_max_chars': (1000, 1000000),
    'file_read_max_chars': (1, 1000000), 'sql_max_rows': (1, 100000),
    'script_timeout_max_seconds': (1, 86400), 'script_default_timeout_seconds': (1, 86400),
    'council_max_tokens': (1, 32000), 'council_timeout_seconds': (1, 3600),
}
SQLITE_OVERRIDES = set(RANGES) | {'log_level', 'council_on_cycle_start'}

def validate(values):
    values = copy.deepcopy(values)
    for key, (low, high) in RANGES.items():
        number = values[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'Invalid {key}: expected number in {low}..{high}')
        if key not in ('context_usage_percent', 'restart_delay_seconds', 'settings_refresh_seconds'):
            if int(number) != number:
                raise ValueError(f'{key} must be integer')
            values[key] = int(number)
    if values['completion_min_tokens'] > values['completion_max_tokens']:
        raise ValueError('Minimum completion exceeds maximum')
    if values['script_default_timeout_seconds'] > values['script_timeout_max_seconds']:
        raise ValueError('Default script timeout exceeds maximum')
    if values['log_level'] not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError('Invalid log_level')
    if not isinstance(values['council_on_cycle_start'], bool):
        raise ValueError('council_on_cycle_start must be boolean')
    if not isinstance(values['servers'], list) or not values['servers']:
        raise ValueError('Configure at least one server entry')
    names, slots = set(), set()
    for server in values['servers']:
        name = server.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Server names must be nonempty and unique')
        names.add(name)
        if not isinstance(server.get('enabled'), bool):
            raise ValueError(f'{name}: enabled must be boolean')
        if not str(server.get('base_url', '')).startswith(('http://', 'https://')):
            raise ValueError(f'{name}: base_url must be HTTP(S)')
        server['base_url'] = server['base_url'].rstrip('/')
        if isinstance(server.get('slot'), bool) or not isinstance(server.get('slot'), int) or server['slot'] < 0:
            raise ValueError(f'{name}: slot must be nonnegative integer')
        if not isinstance(server.get('context_window_fallback'), int) or server['context_window_fallback'] < 2048:
            raise ValueError(f'{name}: invalid context fallback')
        endpoint_slot = (server['base_url'], server['slot'])
        if server['enabled'] and endpoint_slot in slots:
            raise ValueError('Parallel servers must not share the same endpoint and slot')
        if server['enabled']:
            slots.add(endpoint_slot)
    if values['primary_server'] not in names:
        raise ValueError('primary_server must name a configured server')
    if not isinstance(values['extra_allowed_dirs'], list) or not all(isinstance(p, str) for p in values['extra_allowed_dirs']):
        raise ValueError('extra_allowed_dirs must be a list of paths')
    if not isinstance(values['sqlite_path'], str) or not values['sqlite_path']:
        raise ValueError('sqlite_path must be nonempty')
    return values

_settings = validate(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
SQLITE_PATH = (AGENT_ROOT / _settings['sqlite_path']).resolve()
ALLOWED_DIRS = []
_last_refresh = None

def _update_paths():
    extra = [p for p in os.environ.get('AGENT_EXTRA_DIRS', '').split(os.pathsep) if p]
    ALLOWED_DIRS[:] = [str(AGENT_ROOT)] + [str((AGENT_ROOT / p).resolve()) for p in _settings['extra_allowed_dirs'] + extra]

_update_paths()

def get_runtime_settings():
    return copy.deepcopy(_settings)

def reload_settings(store=None, force=False):
    global _settings, _last_refresh
    now = time.monotonic()
    if not force and _last_refresh is not None and now - _last_refresh < _settings['settings_refresh_seconds']:
        return get_runtime_settings()
    _last_refresh = now
    try:
        candidate = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        if (AGENT_ROOT / candidate['sqlite_path']).resolve() != SQLITE_PATH:
            raise ValueError('Changing sqlite_path requires a process restart')
        if store is not None:
            overrides = store.settings()
            unknown = set(overrides) - SQLITE_OVERRIDES
            if unknown:
                raise ValueError('These keys belong in config.json: ' + ', '.join(sorted(unknown)))
            candidate.update(overrides)
        _settings = validate(candidate)
        _update_paths()
    except Exception as exc:
        logging.warning('Settings refresh failed; retaining last good snapshot: %s', exc)
    return get_runtime_settings()

def server_runtime(server, settings=None):
    result = dict(settings or get_runtime_settings())
    result.update(llama_server_url=server['base_url'],
                  llama_api_key=os.environ.get(server.get('api_key_env', ''), server.get('api_key', 'local')),
                  agent_slot=server['slot'], context_window_fallback=server['context_window_fallback'],
                  model_fallback=server.get('model') or 'Qwen')
    return result

def python_executable():
    return os.environ.get('OMEGA_PYTHON') or _settings.get('python_executable') or sys.executable
