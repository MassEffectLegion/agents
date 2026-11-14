"""Agent-local Python runner. File guards are not a sandbox for executed Python."""
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid
from ..core.config import AGENT_ROOT
from .path_guard import require_allowed
from ..core import tg_readonly_guard as _tgro


def _kill_tree(process):
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def run_python_script(script_path: str, script_args: list[str] | None = None,
                      cwd: str | None = None, timeout_sec: float = 60,
                      max_output: int = 20000) -> str:
    """Run a Python file using AGENT_PYTHON (fallback python), without a shell.
    script_path and cwd must pass the same path guard as file tools. script_args is a list.
    Return JSON stdout, stderr, returncode, timed_out, truncated and full_output_path.
    Output limit is in characters across both streams. Full output is retained on disk.
    Python itself runs with the user's permissions; the guard is not a sandbox.
    """
    _g = _tgro.guard_write('run_python_script')
    if _g:
        return _g
    try:
        script = require_allowed(script_path)
        directory = require_allowed(cwd if cwd is not None else AGENT_ROOT)
        if not Path(script).is_file() or not Path(directory).is_dir():
            raise ValueError('script_path must be a file and cwd must be a directory')
        if script_args is None:
            script_args = []
        if not isinstance(script_args, list) or not all(isinstance(arg, str) for arg in script_args):
            raise ValueError('script_args must be a list of strings')
        if not math.isfinite(timeout_sec) or timeout_sec <= 0 or max_output < 0:
            raise ValueError('timeout_sec must be positive and finite; max_output must be nonnegative')
        interpreter = shutil.which(os.environ.get('AGENT_PYTHON') or 'python') or shutil.which('python')
        if not interpreter:
            raise ValueError('Python interpreter not found; set AGENT_PYTHON')
        output_dir = Path(require_allowed(AGENT_ROOT / 'logs' / 'tool_outputs'))
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (uuid.uuid4().hex + '.txt')
        stdout_path = output_path.with_suffix('.stdout.tmp')
        stderr_path = output_path.with_suffix('.stderr.tmp')
        timed_out = False
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        try:
            with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
                process = subprocess.Popen([interpreter, '-u', script, *script_args], cwd=directory,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, shell=False, **options)
                try:
                    process.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_tree(process)
            with output_path.open('wb') as full:
                for label, path in ((b'STDOUT\n', stdout_path), (b'\nSTDERR\n', stderr_path)):
                    full.write(label)
                    with path.open('rb') as stream:
                        shutil.copyfileobj(stream, full)
            with stdout_path.open(encoding='utf-8', errors='replace') as stream:
                stdout = stream.read(max_output + 1)
            remaining = max(0, max_output - len(stdout))
            with stderr_path.open(encoding='utf-8', errors='replace') as stream:
                stderr = stream.read(remaining + 1)
            truncated = len(stdout) > max_output or len(stderr) > remaining
            return json.dumps(dict(stdout=stdout[:max_output], stderr=stderr[:remaining],
                returncode=process.returncode, timed_out=timed_out, truncated=truncated,
                full_output_path=str(output_path)), ensure_ascii=False)
        finally:
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
    except Exception as exc:
        return f'ERROR: run_python_script: {type(exc).__name__}: {exc}'
