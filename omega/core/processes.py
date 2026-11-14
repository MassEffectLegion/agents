"""Own child-process jobs: persistent IDs, logs, timeout, and PID-reuse checks."""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
import psutil
from .config import AGENT_ROOT, get_runtime_settings
from .storage import get_store, now

_children = {}
_lock = threading.Lock()

def _tail(path, max_chars=8000):
    path = Path(path)
    if not path.exists():
        return ''
    with path.open('rb') as handle:
        handle.seek(max(0, path.stat().st_size - max_chars * 4))
        return handle.read().decode('utf-8', errors='replace')[-max_chars:]

def start_job(command, cwd, timeout=None, background=False, store=None):
    if not command or not all(isinstance(arg, str) and '\x00' not in arg for arg in command):
        raise ValueError('Command must be a nonempty list of strings')
    store = store or get_store()
    settings = get_runtime_settings()
    timeout = settings['script_default_timeout_seconds'] if timeout is None else timeout
    if not 0 < timeout <= settings['script_timeout_max_seconds']:
        raise ValueError('Timeout exceeds configured bounds')
    job_id = uuid.uuid4().hex
    log = AGENT_ROOT / 'logs' / 'processes' / (job_id + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    with log.open('wb') as handle:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=handle, stderr=subprocess.STDOUT, shell=False,
                                   creationflags=flags, start_new_session=os.name != 'nt')
    try:
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            created = None
        with store.connection() as conn:
            conn.execute('''INSERT INTO process_jobs(job_id,pid,process_created,started_at,command,cwd,log_path,status)
                            VALUES (?,?,?,?,?,?,?,?)''',
                         (job_id, process.pid, created, now(), json.dumps(command), str(cwd), str(log), 'running'))
    except BaseException:
        process.kill()
        process.wait()
        raise
    with _lock:
        _children[job_id] = process
    if background:
        # Background jobs deliberately have no lifetime timeout; manage with status/stop by job_id.
        return status_job(job_id, store)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_job(job_id, store, status='timed_out')
    return status_job(job_id, store)

def status_job(job_id, store=None):
    store = store or get_store()
    with store.connection(readonly=True) as conn:
        row = conn.execute('SELECT * FROM process_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown job_id; only omega-created jobs are managed')
    row = dict(row)
    with _lock:
        child = _children.get(job_id)
    exit_code = child.poll() if child else None
    if row['status'] == 'running':
        if child and exit_code is not None:
            row.update(status='finished', exit_code=exit_code, finished_at=now())
        elif not child:
            try:
                process = psutil.Process(row['pid'])
                same = row['process_created'] is not None and abs(process.create_time() - row['process_created']) < 0.01
                if not same or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    row.update(status='finished_unknown', finished_at=now())
            except psutil.NoSuchProcess:
                row.update(status='finished_unknown', finished_at=now())
        if row['status'] != 'running':
            with store.connection() as conn:
                conn.execute('UPDATE process_jobs SET status=?,exit_code=?,finished_at=? WHERE job_id=?',
                             (row['status'], row['exit_code'], row['finished_at'], job_id))
    if row['status'] != 'running':
        with _lock:
            _children.pop(job_id, None)
    return {key: row[key] for key in ('job_id', 'pid', 'status', 'exit_code', 'log_path')} | {'output_tail': _tail(row['log_path'])}

def stop_job(job_id, store=None, status='stopped'):
    store = store or get_store()
    current = status_job(job_id, store)
    if current['status'] != 'running':
        return current
    with store.connection(readonly=True) as conn:
        row = conn.execute('SELECT * FROM process_jobs WHERE job_id=?', (job_id,)).fetchone()
    process = psutil.Process(row['pid'])
    if row['process_created'] is None or abs(process.create_time() - row['process_created']) >= 0.01:
        raise RuntimeError('PID no longer belongs to this omega job; refusing to stop it')
    descendants = process.children(recursive=True)
    targets = descendants + [process]
    for target in targets:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=3)
    for target in alive:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)
    with _lock:
        child = _children.get(job_id)
    code = child.poll() if child else None
    with store.connection() as conn:
        conn.execute('UPDATE process_jobs SET status=?,exit_code=?,finished_at=? WHERE job_id=?', (status, code, now(), job_id))
    return status_job(job_id, store)
