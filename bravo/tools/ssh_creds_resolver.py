"""f7d01ae1 v2: SSH-креда авторезолюция из БД asop_access (vault) + mask_secret.
Самодостаточный (workspace, цель: delta3/tools + core alfa/tools после GO).
PG-конфиг: env PG* -> AGENT_SETTINGS_FILE/runtime_settings.local.json.
v2 (07.11, по ревью bravo plan/f7d01ae1_review_bravo.md, verdict OK):
  R1: CIDR-матчинг через PG inet-оператор <<= ("contained in or equal"), не Python/substring;
      точный ip priority; >1 точных совпадений -> ошибка со списком (не молчаливый выбор).
  R2: mask не только в stdout, но и в текстах исключений (PG-пароль маскируется резолвером).
  R3: home-резолв по scope (notes ILIKE '%scope=home%'), host_name-параметр в execute_ssh_command_home.
Безопасность: secret НИКОГДА не выводится (mask_secret). Явные password/key_path имеют приоритет.
"""
import ipaddress
import json
import os
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent  # f7d01ae1 S3: agent-root (delta3|alfa|...), portable
HOME_DEFAULTS = {"ip": "127.0.0.1", "port": 22}


def load_pg_config():
    cfg = {}
    candidates = []
    env_cfg = os.environ.get("AGENT_SETTINGS_FILE")
    if env_cfg:
        candidates.append(Path(env_cfg))
    candidates.append(PKG_ROOT / "runtime_settings.local.json")
    candidates.append(Path(r"<AGENTS_DIR>\main_workdir\alfa\runtime_settings.local.json"))  # legacy fallback
    for local_cfg in candidates:
        if local_cfg.exists():
            try:
                data = json.loads(local_cfg.read_text(encoding="utf-8"))
                cfg = dict(data.get("postgres", {}))
                break
            except Exception:
                continue
    for key, env in {"host": "PGHOST", "port": "PGPORT", "database": "PGDATABASE",
                     "user": "PGUSER", "password": "PGPASSWORD"}.items():
        if env in os.environ:
            cfg[key] = os.environ[env]
    if not cfg.get("host"):
        raise RuntimeError("PG host не найден: runtime_settings.local.json / PGHOST")
    return cfg


def _connect():
    import psycopg2
    cfg = load_pg_config()
    cfg.setdefault("connect_timeout", 10)
    cfg["options"] = (cfg.get("options", "") + " -c statement_timeout=15000").strip()
    return cfg, psycopg2.connect(**cfg)


def _is_valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def mask_secret(text: str, secret: str | None = None) -> str:
    if secret and text:
        text = text.replace(secret, "***")
    return text


def _safe_raise(e: Exception, pg_pw: str | None = None):
    """R2: исключение с замаскированными секретами (PG-пароль и пр.)."""
    msg = str(e)
    if pg_pw:
        msg = msg.replace(pg_pw, "***")
    raise RuntimeError(f"asop_access error: {msg}") from None


def _fetch_match(ip: str):
    """R1: exact (ip ИЛИ hostname) + CIDR через PG inet <<= (contained in or equal). ORDER: exact first."""
    cfg, conn = _connect()
    pg_pw = cfg.get("password")
    try:
        with conn.cursor() as cur:
            # R1: защита от >1 точного совпадения
            cur.execute("SELECT ip, user_name FROM asop_access WHERE is_verified=true AND ip=%s", (ip,))
            exact_rows = cur.fetchall()
            if len(exact_rows) > 1:
                lst = ", ".join(f"{r[0]}({r[1]})" for r in exact_rows)
                _safe_raise(RuntimeError(f"несколько точных совпадений в asop_access для {ip}: {lst}"), pg_pw)
            # R1: CIDR через inet-оператор <<= ("left contained in right").
            # split_part — строка с комментарием: '10.0.0.1/16 (АСОП...)' -> '10.0.0.1/16'.
            sql = ("SELECT ip, port, user_name, secret, auth_method, is_verified, notes FROM asop_access "
                   "WHERE is_verified = true AND secret IS NOT NULL "
                   "AND (ip = %s")
            params: list = [ip]
            if _is_valid_ip(ip):
                sql += (" OR (position('/' in ip) > 0 "
                        "AND %s::inet <<= split_part(ip, ' ', 1)::inet)")
                params.append(ip)
            sql += ") ORDER BY (ip = %s) DESC, id LIMIT 1"
            params.append(ip)
            cur.execute(sql, params)
            return cur.fetchone()
    except RuntimeError:
        raise
    except Exception as e:
        _safe_raise(e, pg_pw)
    finally:
        conn.close()
    return None


def resolve_ssh_creds(ip: str, port: int | None = None):
    """Авторезолюция SSH-кредов из asop_access: ТОЧНОЕ (ip/hostname) -> CIDR (pg inet <<=).
    Возврат: {'ip','port','user','secret','auth_method','matched_by'} или None."""
    row = _fetch_match(ip)
    if row is None:
        return None
    row_ip, mport, user, secret, auth, _ver, notes = row
    return {
        "ip": ip,
        "port": port or mport or 22,
        "user": user,
        "secret": secret,
        "auth_method": auth or "password",
        "matched_by": "exact" if row_ip == ip else f"cidr:{row_ip.split()[0]}",
        "notes": (notes or "")[:200],
    }


def resolve_home_creds(scope: str = "home"):
    """R3: home-резолв по scope (notes ILIKE '%scope=<scope>%'), fallback на 127.0.0.1."""
    cfg, conn = _connect()
    pg_pw = cfg.get("password")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ip, port, user_name, secret, auth_method, notes FROM asop_access "
                "WHERE is_verified=true AND secret IS NOT NULL AND notes ILIKE %s "
                "ORDER BY id LIMIT 1", (f"%scope={scope}%",))
            row = cur.fetchone()
    except Exception as e:
        _safe_raise(e, pg_pw)
    finally:
        conn.close()
    if row is None:
        return resolve_ssh_creds(HOME_DEFAULTS["ip"], HOME_DEFAULTS["port"])
    row_ip, mport, user, secret, auth, notes = row
    return {
        "ip": row_ip,
        "port": mport or 22,
        "user": user,
        "secret": secret,
        "auth_method": auth or "password",
        "matched_by": f"scope={scope}",
        "notes": (notes or "")[:200],
    }


def get_home_creds():
    """Backward-compat обёртка (v1) — scope=home."""
    c = resolve_home_creds("home")
    if c is None:
        raise RuntimeError("Креды home-сервера не найдены в asop_access (scope=home)")
    return c
