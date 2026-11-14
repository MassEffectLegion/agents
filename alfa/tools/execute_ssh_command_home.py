"""f7d01ae1 v2: execute_ssh_command_home — SSH на домашний сервер БЕЗ явных кредов.
Креды подтягиваются автоматически из БД asop_access (строка с notes scope=home; fallback 127.0.0.1).
Самодостаточный (workspace): paramiko + ssh_creds_resolver. Цель: delta3/tools (после GO) + core alfa/tools.
v2 (07.11, ревью bravo R3): host_name-параметр (scope) вместо захардкоженного 1.88.
Secret никогда не выводится (mask_secret).
"""
import os
from datetime import datetime
from pathlib import Path

import paramiko

from alfa.tools import ssh_creds_resolver as R  # f7d01ae1 S3: package import (delta3.tools.*)

DEFAULT_LOG_DIR = str(Path(__file__).resolve().parent.parent / "logs")  # f7d01ae1 S3: agent logs dir


def execute_ssh_command_home(
    command: str,
    output_handling: str = "auto",
    save_dir: str = DEFAULT_LOG_DIR,
    timeout: int = 60,
    host_name: str = "home",
) -> str:
    """Execute a bash command on the HOME server — NO creds needed.
    Auth auto-resolved from DB asop_access (row notes scope=<host_name>; default 'home' = 127.0.0.1).
    secret never printed (masked).
    Args:
        command: Bash command to execute (no auto-sudo; add 'sudo ' in command if needed).
        output_handling: "auto" (>4000 chars -> file) | "save_to_file" | "full".
        save_dir: Directory for saved output files.
        timeout: SSH command timeout seconds.
        host_name: scope in asop_access notes (default 'home' = домашний ubuntu 127.0.0.1).
    """
    creds = R.resolve_home_creds(host_name)
    if creds is None:
        return f"SSH-HOME ERROR: креды scope={host_name} не найдены в asop_access"
    secret = creds["secret"]
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=creds["ip"], port=creds["port"], username=creds["user"],
            password=secret, timeout=15, allow_agent=False, look_for_keys=False,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
    except Exception as e:
        return R.mask_secret(f"SSH-HOME ERROR: {e}", secret)
    finally:
        if client is not None:
            client.close()

    out = R.mask_secret(out, secret)
    err = R.mask_secret(err, secret)
    if output_handling == "auto" and len(out) > 4000:
        output_handling = "save_to_file"

    if output_handling == "save_to_file":
        os.makedirs(save_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(save_dir, f"ssh_home_{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# SSH HOME {creds['ip']}:{creds['port']} (creds auto from DB asop_access, matched_by={creds['matched_by']})\n")
            f.write(f"# Command: {command}\n# Exit Code: {exit_code}\n# {ts}\n")
            f.write("=" * 60 + "\n\n=== STDOUT ===\n" + out)
            if err:
                f.write("\n\n=== STDERR ===\n" + err)
        return (f"✅ exit_code={exit_code} | {len(out)} chars\n"
                f"💾 SAVED: {path}\n"
                f"(creds: auto from DB asop_access, user={creds['user']}, masked)")

    result = f"STDOUT (exit code: {exit_code}, home={creds['ip']}:{creds['port']}, user={creds['user']}, matched_by={creds['matched_by']}):\n{out}"
    if err:
        result += f"\n\nSTDERR:\n{err}"
    return result
