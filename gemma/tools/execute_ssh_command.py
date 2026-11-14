from gemma.tools.path_guard import require_allowed
from gemma.tools import ssh_creds_resolver as _res  # f7d01ae1 (GO 07.11)
import paramiko
import os
from datetime import datetime
# P1-5 (rev 1.2, 2026-09-22, OK человека b3388012): свой SSH-хелпер alfa
# (был `from devops_agent.core.ssh_helper import _create_ssh_client` и
# `from devops_agent.config import LOG_DIR` — зависимость от пакета чужого агента).
from gemma.core.ssh_helper import _create_ssh_client
from gemma.core.config import LOG_DIR
from gemma.core import tg_readonly_guard as _tgro

def execute_ssh_command(
    ip: str,
    port: int,
    user: str = "",  # f7d01ae1: пусто -> авто из asop_access
    password: str | None = None,
    key_path: str | None = None,
    command: str = "",
    output_handling: str = "auto",
    save_dir: str = LOG_DIR
) -> str:
    """Execute a bash command on a remote server via SSH.
    Auth priority: (1) явные креды (key_path/password+user) — как раньше (backward-compat);
    (2) если user+password+key_path не переданы — АВТОРЕЗОЛЮЦИЯ из БД asop_access (f7d01ae1, GO 07.11).
    S3: tg_readonly_guard (guest/project -> read-only white-list) — сохранён (нет в шаблоне f7d01ae1).
    Args:
        ip: Server IP address
        port: SSH port
        user: SSH username (пусто/None -> авторезолюция из БД asop_access, f7d01ae1)
        password: SSH password (used if no key_path provided; None -> авто из asop_access)
        key_path: Path to SSH private key file (preferred auth method)
        command: Bash command to execute
        output_handling: How to handle long output:
            - "auto": if >4000 chars -> save to file
            - "save_to_file": ALWAYS save to file (for configs, netstat, lists)
            - "full": return the complete raw output (may be very large)
        save_dir: Directory for saved output files
    """
    save_dir = require_allowed(save_dir)
    _g = _tgro.guard_ssh(command)
    if _g:
        return _g

    # f7d01ae1 (GO человека 07.11): авторезолюция кредов из asop_access, если явных нет.
    resolved_secret = None
    matched_by = None
    if not user and password is None and key_path is None:
        try:
            rc = _res.resolve_ssh_creds(ip, port)
        except Exception as e:
            return f"SSH CRED-RESOLVE ERROR (asop_access): {e}"
        if rc is None:
            return (f"SSH CRED-RESOLVE: для {ip}:{port} нет кредов в asop_access "
                    f"(is_verified=true). Передайте user+password/key_path явно.")
        port = rc["port"]
        user = rc["user"]
        key_path = rc["secret"] if rc["auth_method"] == "key_path" else None
        password = rc["secret"] if key_path is None else None
        resolved_secret = rc["secret"]
        matched_by = rc["matched_by"]
    if not user:
        return f"SSH ERROR: не указан user (и нет авто-кредов для {ip}:{port})"

    client = None
    sftp = None
    try:
        client = _create_ssh_client(ip, port, user, password=password, key_path=key_path)
        stdin, stdout, stderr = client.exec_command(command, timeout=60)
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()

        # f7d01ae1: secret никогда не попадает в вывод
        out = _res.mask_secret(out, resolved_secret)
        err = _res.mask_secret(err, resolved_secret)

        # Only auto-detect output_handling when user didn't explicitly set it
        if output_handling == "auto":
            cmd_lower = command.strip().lower()
            cmd_parts = cmd_lower.split()
            cmd_name = cmd_parts[0] if cmd_parts else ""

            # Structured commands: use save_to_file for tabular/network data
            structured_commands = ['netstat', 'ss ', 'cat /etc', 'ls -l', 'df ', 'du ', 'find ']
            if any(cmd in cmd_lower for cmd in structured_commands):
                output_handling = "save_to_file"

            # Explicitly handle 'ps' and 'psql' separately
            if cmd_name == 'ps' or cmd_lower.startswith('ps ') or cmd_lower == 'ps':
                output_handling = "save_to_file"

        out_lines = len(out.split('\n')) if out else 0
        out_len = len(out)
        err_len = len(err)

        should_save = False

        if output_handling == "save_to_file":
            should_save = True
        elif output_handling == "full":
            pass
        elif output_handling == "auto":
            if out_len > 4000:
                should_save = True

        # Mode 1: Save to file
        if should_save:
            os.makedirs(save_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"ssh_{ip.replace('.', '_')}_{timestamp}.txt"
            filepath = os.path.join(save_dir, filename)

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"# SSH Command Output\n")
                f.write(f"# Server: {ip}:{port}\n")
                f.write(f"# Command: {command}\n")
                f.write(f"# Exit Code: {exit_code}\n")
                f.write(f"# Timestamp: {timestamp}\n")
                f.write(f"# Lines: {out_lines}\n")
                f.write(f"{'='*60}\n\n")
                f.write("=== STDOUT ===\n")
                f.write(out)
                if err:
                    f.write("\n\n=== STDERR ===\n")
                    f.write(err)

            return (
                f"✅ exit_code={exit_code} | {out_len} chars, {out_lines} lines\n"
                f"💾 SAVED: {filepath}"
            )

        # Mode 2: Full output
        result = f"STDOUT (exit code: {exit_code}):\n{out}"
        if err:
            result += f"\n\nSTDERR:\n{err}"
        return result

    except Exception as e:
        return _res.mask_secret(f"SSH ERROR: {str(e)}", resolved_secret)

    finally:
        try:
            if sftp is not None:
                sftp.close()
        finally:
            if client is not None:
                client.close()
