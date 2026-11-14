# -*- coding: utf-8 -*-
"""vault_home.py — инструмент к home Vault 127.0.0.1:8200 (KV-v2, mount agents_secrets).

ГО человека (HR 153148ce, 12.11): "vault_home также вынести в постоянно действующий
каталог tools/ + vault_home — инструмент к vault 1.88 (KV2, /v1/agents_secrets/data/agents/<key>)
+ перенос credential-хранилища в home vault".

КАК РАБОТАЕТ:
  Vault слушает ТОЛЬКО 127.0.0.1:8200 на 1.88 -> vault-операции выполняются НА 1.88
  через SSH (paramiko; креды — ssh_creds_resolver.resolve_home_creds, scope=home).
  Bootstrap-токен: /home/agents/.vault_bootstrap_token (perms 600; источник c543_vault_items
  bravo, ключ vault/home_1_88_token). SSH-пароль маскируется во всём выводе.

ИСПОЛЬЗОВАНИЕ:
  python vault_home.py health
  python vault_home.py ls [agents/...]            # список ключей (без значений)
  python vault_home.py get <key> [поле]           # значение; key без mount или 'agents_secrets/...'
  python vault_home.py put <key> <значение>       # запись value-поля
  python vault_home.py putjson <key> <json>       # запись произвольного объекта
  python vault_home.py migrate-credentials [dryrun]   # agents_mobilpay.credentials -> vault
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ssh_creds_resolver import resolve_home_creds, mask_secret  # noqa

DEFAULT_MOUNT = "agents_secrets"
TOKEN_FILE = "/home/agents/.vault_bootstrap_token"
HOME_IP = "127.0.0.1"
HOME_PORT = 22
VAULT_ADDR = "http://127.0.0.1:8200"

_CFG = r"<AGENTS_DIR>\main_workdir\alfa\runtime_settings.local.json"


def _pg_connect():
    import psycopg2
    cfg = json.load(open(_CFG, encoding="utf-8"))["postgres"]
    return cfg, psycopg2.connect(host=cfg["host"], port=cfg.get("port", 5432),
                                 dbname=cfg["database"], user=cfg["user"],
                                 password=cfg["password"], connect_timeout=10)


def run_remote(command, timeout=90):
    """Выполняет bash на 1.88. Возвращает (rc, out); SSH-секрет маскируется."""
    import paramiko
    rc = resolve_home_creds("home")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if rc["auth_method"] == "key_path":
        client.connect(HOME_IP, port=HOME_PORT, username=rc["user"], key_filename=rc["secret"], timeout=15)
    else:
        client.connect(HOME_IP, port=HOME_PORT, username=rc["user"], password=rc["secret"], timeout=15)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, mask_secret(out, rc["secret"]) + (("\n[stderr] " + mask_secret(err, rc["secret"])) if err else "")
    finally:
        client.close()


def run_remote_helper(py_code, stdin_json=None, timeout=90):
    """Выполняет python-хелпер на 1.88 (base64 -> /tmp), stdin-JSON передаётся файлом.
    Возвращает (rc, stdout)."""
    b64 = base64.b64encode(py_code.encode("utf-8")).decode("ascii")
    data_b64 = base64.b64encode((stdin_json or "").encode("utf-8")).decode("ascii") if stdin_json else ""
    cmd = ("echo %s | base64 -d > /tmp/_vh_helper.py; " % b64)
    if data_b64:
        cmd += "echo %s | base64 -d > /tmp/_vh_data.json; " % data_b64
        cmd += "python3 /tmp/_vh_helper.py /tmp/_vh_data.json 2>&1; rc=$?; rm -f /tmp/_vh_helper.py /tmp/_vh_data.json; exit $rc"
    else:
        cmd += "python3 /tmp/_vh_helper.py 2>&1; rc=$?; rm -f /tmp/_vh_helper.py; exit $rc"
    return run_remote(cmd, timeout=timeout)


# --- remote helper templates (run ON 1.88, python3) ---
HELP_LS = r'''
import sys, json, urllib.request
tok = open("/home/agents/.vault_bootstrap_token").read().strip()
prefix = sys.argv[1] if len(sys.argv) > 1 else ""
path = "http://127.0.0.1:8200/v1/agents_secrets/metadata/" + prefix
req = urllib.request.Request(path, headers={"X-Vault-Token": tok})
try:
    d = json.load(urllib.request.urlopen(req, timeout=20))
    keys = d.get("data", {}).get("keys", [])
    print("n_keys=" + str(len(keys)))
    for k in keys:
        print("  " + k)
except urllib.error.HTTPError as e:
    print("HTTP %s: %s" % (e.code, e.read().decode("utf-8","replace")[:200]))
'''

HELP_GET = r'''
import sys, json, urllib.request
datafile = sys.argv[1]
tok = open("/home/agents/.vault_bootstrap_token").read().strip()
args = json.load(open(datafile))
key, field = args["key"], args.get("field")
path = "http://127.0.0.1:8200/v1/agents_secrets/data/" + key
req = urllib.request.Request(path, headers={"X-Vault-Token": tok})
try:
    d = json.load(urllib.request.urlopen(req, timeout=20))
except urllib.error.HTTPError as e:
    print("HTTP %s: %s" % (e.code, e.read().decode("utf-8","replace")[:200])); sys.exit(0)
data = d.get("data", {}).get("data", {})
if field:
    v = data.get(field)
    print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
else:
    print(json.dumps(data, ensure_ascii=False))
'''

HELP_PUT = r'''
import sys, json, urllib.request
datafile = sys.argv[1]
tok = open("/home/agents/.vault_bootstrap_token").read().strip()
args = json.load(open(datafile))
payload = json.dumps({"data": args["obj"]}).encode()
path = "http://127.0.0.1:8200/v1/agents_secrets/data/" + args["key"]
req = urllib.request.Request(path, data=payload, method="POST",
                             headers={"X-Vault-Token": tok, "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=20)
    print("HTTP %s OK" % r.status)
except urllib.error.HTTPError as e:
    print("HTTP %s: %s" % (e.code, e.read().decode("utf-8","replace")[:200]))
'''


def cmd_health(mount):
    rc, out = run_remote("curl -s %s/v1/sys/health" % VAULT_ADDR)
    print(out)


def cmd_ls(key_prefix, mount):
    # prefix передаётся аргументом helper'у (HELP_LS читает sys.argv[1])
    rc, out = run_remote("T=$(cat %s); echo %s | base64 -d > /tmp/_vh_helper.py; "
                         "python3 /tmp/_vh_helper.py %s 2>&1; rc=$?; rm -f /tmp/_vh_helper.py; exit $rc"
                         % (TOKEN_FILE, _b64(HELP_LS), _q(key_prefix)))
    print(out)


def _b64(s):
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_get(key, field, mount):
    args = json.dumps({"key": key, "field": field})
    rc, out = run_remote_helper(HELP_GET, stdin_json=args)
    print(out)


def cmd_put(key, value, mount):
    args = json.dumps({"key": key, "obj": {"value": value}})
    rc, out = run_remote_helper(HELP_PUT, stdin_json=args)
    print(out)


def cmd_putjson(key, js, mount):
    obj = json.loads(js)
    args = json.dumps({"key": key, "obj": obj})
    rc, out = run_remote_helper(HELP_PUT, stdin_json=args)
    print(out)


def cmd_migrate_credentials(dryrun):
    """Перенос credential-хранилища agents_mobilpay.credentials -> home vault 1.88.
    Vault-ключ: agents/credentials/<service_name> = {url,login,secret,auth_type,context,status,...}."""
    import datetime
    cfg, conn = _pg_connect()
    cur = conn.cursor()
    cur.execute("SELECT service_name, url, login, secret, auth_type, context, status "
                "FROM agents_mobilpay.credentials ORDER BY service_name")
    rows = cur.fetchall()
    conn.close()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("credentials rows: %d" % len(rows))
    if dryrun:
        for r in rows:
            print("  DRYRUN %s -> agents/credentials/%s (url,login,secret(%dB),auth_type,context,status)"
                  % (r[0], r[0], len(r[3] or "")))
        return
    for r in rows:
        svc, url, login, secret, auth, context, status = r
        obj = {"url": url, "login": login, "secret": secret, "auth_type": auth,
               "context": context, "status": status,
               "migrated_from": "agents_mobilpay.credentials", "migrated_at": ts}
        args = json.dumps({"key": "agents/credentials/%s" % svc, "obj": obj})
        rc, out = run_remote_helper(HELP_PUT, stdin_json=args)
        print("  %s -> %s" % (svc, out.strip()))
    cmd_ls("agents/credentials", DEFAULT_MOUNT)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "health":
        cmd_health(DEFAULT_MOUNT)
    elif cmd == "ls":
        cmd_ls(sys.argv[2] if len(sys.argv) > 2 else "", DEFAULT_MOUNT)
    elif cmd == "get":
        field = sys.argv[3] if len(sys.argv) > 3 else None
        cmd_get(sys.argv[2], field, DEFAULT_MOUNT)
    elif cmd == "put":
        cmd_put(sys.argv[2], sys.argv[3], DEFAULT_MOUNT)
    elif cmd == "putjson":
        cmd_putjson(sys.argv[2], sys.argv[3], DEFAULT_MOUNT)
    elif cmd == "migrate-credentials":
        cmd_migrate_credentials(len(sys.argv) < 3 or sys.argv[2] == "dryrun")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
