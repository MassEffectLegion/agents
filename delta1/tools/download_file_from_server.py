from delta1.tools.path_guard import require_allowed
import paramiko
import os
# P1-5 (rev 1.2, 2026-09-22, OK человека b3388012): свой SSH-хелпер delta1
# (был `from devops_agent.core.ssh_helper import _create_ssh_client` —
# зависимость от пакета чужого агента).
from delta1.core.ssh_helper import _create_ssh_client
from delta1.core import tg_readonly_guard as _tgro

def download_file_from_server(
    ip: str, port: int, user: str, remote_path: str, local_path: str,
    key_path: str | None = None, password: str | None = None
) -> str:
    """Download a file from a remote server via SFTP.
    Args:
        ip: Server IP address
        port: SSH port
        user: SSH username
        remote_path: Remote file path
        local_path: Local file destination path
        key_path: SSH private key (optional)
        password: SSH password (optional)
    """
    _g = _tgro.guard_write('download_file_from_server')
    if _g:
        return _g
    local_path = require_allowed(local_path)
    client = None
    sftp = None
    try:
        client = _create_ssh_client(ip, port, user, password=password, key_path=key_path, timeout=30)
        sftp = client.open_sftp()
        parent = os.path.dirname(local_path)
        if parent:  # P2-14: dirname может быть '' для relative-имени
            os.makedirs(parent, exist_ok=True)
        sftp.get(remote_path, local_path)
        return f"✓ downloaded: {local_path}"
    except Exception as e:
        return f"ERROR: SFTP download: {str(e)}"

    finally:
        try:
            if sftp is not None:
                sftp.close()
        finally:
            if client is not None:
                client.close()
