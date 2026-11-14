"""Recovery tools backed only by SQLite and local subprocesses."""
import json
import os
import uuid
from pathlib import Path
from langchain_core.tools import tool
from omega.core.config import AGENT_ROOT, get_runtime_settings, python_executable
from omega.core.storage import get_store
from omega.core.processes import start_job, status_job, stop_job
from omega.tools.path_guard import require_allowed

def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)

@tool
def query_sqlite(query: str, parameters_json: str = '[]', max_rows: int = 100, allow_write: bool = False) -> str:
    """Execute one SQL statement in omega's SQLite. Use ? placeholders and JSON parameters.
    SELECT/CTE/RETURNING produce JSON rows. Explicit allow_write=True enables writes.
    Other databases cannot be attached. Use LIMIT for large histories.
    """
    try:
        parameters = json.loads(parameters_json)
        if not isinstance(parameters, (list, dict)):
            raise ValueError('Parameters must be a JSON array or object')
        return _json(get_store().query(query, parameters, max(1,min(max_rows,get_runtime_settings()['sql_max_rows'])), allow_write))
    except Exception as exc:
        return f'SQLite ERROR: {exc}'

@tool
def save_progress(done: str, results: str, remaining: str) -> str:
    """Save durable progress and remaining steps to SQLite and a local JSON file."""
    task_id = os.environ.get('AGENT_TASK_ID')
    store = get_store()
    if not task_id:
        task_id, _ = store.resume_or_create()
    payload = {'done':[done], 'key_facts':[results], 'remaining':[remaining]}
    path = AGENT_ROOT / 'data' / 'progress' / f'{task_id}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding='utf-8')
    payload['artifacts'] = [str(path)]
    store.checkpoint(task_id, payload, 'save_progress')
    store.artifact(task_id, 'progress', path)
    return f'Saved SQLite checkpoint and {path}'

@tool
def complete_request(request_id: str, result: str) -> str:
    """Mark a local human request done and store its actual result for the owner."""
    get_store().complete_request(request_id, result)
    return 'Request completed: ' + request_id

@tool
def backup_sqlite(destination: str = '') -> str:
    """Create an online SQLite backup including committed WAL data; verify its integrity."""
    path = require_allowed(destination) if destination else str(AGENT_ROOT/'data'/'backups'/f'omega-{uuid.uuid4().hex}.sqlite3')
    return get_store().backup(path)

@tool
def run_python_script(code: str = '', script_path: str = '', arguments: list[str] = None,
                      working_directory: str = '', interpreter: str = '',
                      timeout_seconds: int = 60, background: bool = False) -> str:
    """Run Python code OR a .py file, with output saved to a log. Exactly one of code/script_path.
    Use background=True for launching long-lived agents; returns job_id for status_process/stop_process.
    Foreground calls have a timeout; background jobs run until they exit or are explicitly stopped.
    interpreter selects a Python/venv executable. Scripts run with this user's OS permissions.
    """
    if bool(code) == bool(script_path):
        raise ValueError('Provide exactly one of code or script_path')
    if code:
        path = AGENT_ROOT / 'data' / 'scripts' / (uuid.uuid4().hex + '.py')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding='utf-8')
    else:
        path = Path(require_allowed(script_path))
        if path.suffix.lower() != '.py' or not path.is_file():
            raise ValueError('script_path must be an existing .py file')
    cwd = require_allowed(working_directory) if working_directory else str(AGENT_ROOT)
    launcher = [interpreter or python_executable()]
    if not interpreter:
        launcher += ['-I', '-X', 'utf8']
    launcher.append('-u')
    if not interpreter:
        launcher.append(str(AGENT_ROOT / 'boot.py'))
    return _json(start_job(launcher + [str(path)] + (arguments or []), cwd, timeout_seconds, background))

@tool
def run_command(arguments: list[str], working_directory: str = '', timeout_seconds: int = 60,
                background: bool = False) -> str:
    """Run an explicit executable and argument list (shell=False), e.g. python -m pip, git, psql.
    For shell syntax explicitly invoke powershell.exe/cmd.exe/bash. Output goes to a log.
    Background jobs return a job_id; foreground jobs stop their process tree on timeout.
    """
    cwd = require_allowed(working_directory) if working_directory else str(AGENT_ROOT)
    return _json(start_job(arguments, cwd, timeout_seconds, background))

@tool
def status_process(job_id: str) -> str:
    """Read status, exit code and recent output of a process launched by omega."""
    return _json(status_job(job_id))

@tool
def stop_process(job_id: str) -> str:
    """Stop an omega-created process and its children. Verifies PID ownership before stopping."""
    return _json(stop_job(job_id))

@tool
def consult_models(question: str, context: str = '', server_names: list[str] = None) -> str:
    """Ask DENSE/MoE servers in parallel for independent advice. They cannot execute tools.
    Supply concise evidence/context. Results identify each server and any failures.
    """
    from omega.core.models import get_active_pool
    results = get_active_pool().consult(question, context, server_names)
    path = AGENT_ROOT/'logs'/'consultations'/f'{uuid.uuid4().hex}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(results), encoding='utf-8')
    return _json({'responses':results, 'saved':str(path)})
