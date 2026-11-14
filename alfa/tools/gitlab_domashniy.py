# -*- coding: utf-8 -*-
"""gitlab_domashniy.py — workspace-инструмент (17a0e9af, rest#15, 12.11).

Домашний GitLab CE 19.5.3 на 127.0.0.1 (web http://127.0.0.1, проекты:
root/transcard, root/asukat, root/main_agents, root/asop-max).
Креды — ТОЛЬКО из БД agents_mobilpay.credentials (service_name='gitlab_domashniy'),
НИКАКОГО хардкода (идея человека из HR f32ef82e; паттерн как у execute_ssh_command_home
— autorezolyutsiya из asop_access).

Использование:
  python gitlab_domashniy.py projects
  python gitlab_domashniy.py refs root/main_agents
  python gitlab_domashniy.py push <локальный каталог git> <project> <ветка>
  python gitlab_domashniy.py clone <project> <целевой каталог>

После ревью человеком — кандидат на core (tools_registry category='core').
"""
import json, os, subprocess, sys, urllib.request, urllib.parse, base64, ssl

CFG = r"<AGENTS_DIR>\main_workdir\alfa\runtime_settings.local.json"

def db_creds(service_name):
    """Читает (login, secret, url) из БД agents_mobilpay.credentials. Без хардкода."""
    import psycopg2
    cfg = json.load(open(CFG, encoding="utf-8"))["postgres"]
    conn = psycopg2.connect(host=cfg["host"], port=cfg.get("port", 5432), dbname=cfg["database"],
                            user=cfg["user"], password=cfg["password"],
                            options="-c search_path=agents_mobilpay", connect_timeout=10)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, login, secret FROM agents_mobilpay.credentials "
        "WHERE service_name=%s AND status='active' LIMIT 1", (service_name,))
    row = cur.fetchone()
    conn.close()
    if not row:
        sys.exit("ERR: credentials-строка '%s' не найдена (status=active)" % service_name)
    return row[0], row[1], row[2]

def gitlab_headers(secret):
    # GitLab CE: http basic (root) или private token — у нас basic root
    return {"Authorization": "Basic " + base64.b64encode(("root:" + secret).encode()).decode()}

def gl(url_base, path, secret, method="GET", data=None):
    url = url_base + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=gitlab_headers(secret))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def git_cmd(cmd, timeout=180):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def main():
    url_base, login, secret = db_creds("gitlab_domashniy")
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == "projects":
        s, b = gl(url_base, "/api/v4/projects?per_page=100", secret)
        print("status=%s" % s)
        if s == 200:
            for p in json.loads(b):
                print("  %s | default=%s | visibility=%s" % (p["path_with_namespace"], p.get("default_branch"), p.get("visibility")))
    elif cmd == "refs":
        project = urllib.parse.quote_plus(sys.argv[2])
        s, b = gl(url_base, "/api/v4/projects/%s/repository/branches?per_page=50" % project, secret)
        print("status=%s branches:" % s)
        if s == 200:
            for br in json.loads(b):
                print("  %s -> %s" % (br["name"], br["commit"]["short_id"]))
    elif cmd == "push":
        local, project, branch = sys.argv[2], sys.argv[3], sys.argv[4]
        remote = "%s/root/%s.git" % (url_base, project.replace("root/", ""))
        auth_url = "http://%s:%s@%s/root/%s.git" % (login, secret.replace("!", "%21"), url_base.replace("http://", ""), project.replace("root/", ""))
        rc, out, err = git_cmd("git -C %s remote set-url __homegl %s || git -C %s remote add __homegl %s" % (local, auth_url, local, auth_url))
        rc, out, err = git_cmd("git -C %s push -u __homegl %s" % (local, branch), timeout=300)
        print("push rc=%d\nOUT: %s\nERR: %s" % (rc, out[:300], err[:300]))
    elif cmd == "clone":
        project, dest = sys.argv[2], sys.argv[3]
        auth_url = "http://%s:%s@%s/root/%s.git" % (login, secret.replace("!", "%21"), url_base.replace("http://", ""), project.replace("root/", ""))
        rc, out, err = git_cmd("git clone --depth 1 %s %s" % (auth_url, dest), timeout=300)
        print("clone rc=%d\nOUT: %s\nERR: %s" % (rc, out[:300], err[:300]))
        # убрать кред из config
        if os.path.isdir(dest):
            git_cmd("git -C %s remote set-url origin %s/root/%s.git" % (dest, url_base, project.replace("root/", "")))
            print("origin-url scrubbed (без кредов)")
    else:
        print("unknown cmd:", cmd); print(__doc__)

if __name__ == "__main__":
    main()
    print("GITLAB_DOMASHNIY DONE")
