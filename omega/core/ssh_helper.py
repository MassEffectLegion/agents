"""Restored from devops_agent helper; close failures and support key/password fallback."""
import paramiko

def _create_ssh_client(host, port, user, password=None, key_path=None, timeout=30):
    attempts = [{'key_filename': key_path}] if key_path else []
    if password:
        attempts.append({'password': password})
    if not attempts:
        raise ValueError('Neither SSH key nor password provided')
    last_error = None
    for auth in attempts:
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(host, port=int(port), username=user, timeout=timeout,
                auth_timeout=timeout, banner_timeout=timeout, allow_agent=False, look_for_keys=False, **auth)
            return client
        except Exception as exc:
            last_error = exc
            client.close()
    raise paramiko.SSHException(f'SSH connection failed for {user}@{host}:{port}: {last_error}') from last_error
