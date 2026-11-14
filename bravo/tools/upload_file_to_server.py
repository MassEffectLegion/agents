from bravo.tools.path_guard import require_allowed
import paramiko
import os
from bravo.core.ssh_helper import _create_ssh_client

def upload_file_to_server(
    ip: str, port: int, user: str, local_path: str, remote_path: str,
    key_path: str | None = None, password: str | None = None
) -> str:
    """Upload a local file to a remote server via SFTP.
    Args:
        ip: Server IP address
        port: SSH port
        user: SSH username
        local_path: Local file path to upload
        remote_path: Remote destination path
        key_path: SSH private key (optional)
        password: SSH password (optional)
    """
    if not os.path.exists(local_path):
        return f"ERROR: Local file not found: {local_path}"
    local_path = require_allowed(local_path)
    client = None
    sftp = None
    try:
        client = _create_ssh_client(ip, port, user, password=password, key_path=key_path, timeout=30)
        sftp = client.open_sftp()

        remote_dir = os.path.dirname(remote_path).replace('\\', '/')
        if remote_dir:
            _makedirs_sftp(sftp, remote_dir)

        sftp.put(local_path, remote_path)
        return f"✓ uploaded: {remote_path}"
    except Exception as e:
        return f"ERROR: SFTP upload: {str(e)}"

    finally:
        try:
            if sftp is not None:
                sftp.close()
        finally:
            if client is not None:
                client.close()

def _makedirs_sftp(sftp_client: paramiko.SFTPClient, remote_dir: str) -> None:
    """Create remote directory recursively."""
    path_parts = []
    for part in filter(None, remote_dir.split('/')):
        path_parts.append(part)
        current_path = ('/' if remote_dir.startswith('/') else '') + '/'.join(path_parts)
        try:
            sftp_client.stat(current_path)
        except FileNotFoundError:
            try:
                sftp_client.mkdir(current_path)
            except OSError:
                pass
