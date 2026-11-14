# -*- coding: utf-8 -*-
"""gemma / core/db_access.py — shared-хелпер подключения к БД для SQL-инструментов.

GO-2 (HR 76ac44b6, GO человека; P1 согласован с alfa mailbox id=40).
Интегрирует core/db_failover.py (draft alfa, selftest gemma PASS 3/3) в 3 SQL-инструмента
tools/query_postgres_local.py, tools/get_table_schema.py, tools/list_postgres_tables.py.

ЛОГИКА:
  - runtime_settings.local.json: postgres.databases[] + failover.enabled=true
    -> core.db_failover.DBFailover (sticky primary->local, reprobe, back-switch).
  - ИНАЧЕ (failover выключен) -> СТАРАЯ логика: одиночный host (backward-compat:
    поведение инструментов не меняется).
  - P3 (согласовано alfa id=40, пункт 3): mark_dirty() — tools при allow_write=True,
    когда active != primary, пишут/обновляют dirty-маркер
    (logs/failover_dirty.json) — его читает pgmerge apply-gate
    (workspace/tools/b2c1ff69/pgmerge_355_to_177.py, FAIL_MARKER).
  - on_error(exc) — в except-обработчиках tools; best-effort back-switch на primary.

АКТИВАЦИЯ: только runtime_settings.local.json failover.enabled=true. До этого — no-op.
Ревью: gitlab 1.88 root/main_agents ветка gemma (MR/branch-review alfa+bravo).
"""
import json
import logging
import time

import psycopg2

from .config import AGENT_ROOT, PG_CONFIG_RAW as PG_CONFIG  # B6 c13: raw section (databases/failover)
from .db_failover import get as failover_get

log = logging.getLogger("db_access")

_FAILOVER = None

# P3: dirty-маркер (совместим с pgmerge FAIL_MARKER). Путь можно переопределить
# в runtime_settings: postgres.failover.dirty_marker_path.
DEFAULT_DIRTY_MARKER = str(AGENT_ROOT / "logs" / "failover_dirty.json")
DIRTY_MARKER_MAX_AGE_S = 30 * 60  # 30 мин (pgmerge: stale > 30 мин -> refuse)


def _pg_cfg() -> dict:
    """postgres-секция runtime_settings.local.json (PG_CONFIG = она же + env override)."""
    return dict(PG_CONFIG)


def failover_active() -> bool:
    """True, если в конфиге есть databases[] И failover.enabled=true."""
    cfg = _pg_cfg()
    return bool(cfg.get("databases")) and bool(cfg.get("failover", {}).get("enabled", False))


def _marker_path() -> str:
    cfg = _pg_cfg()
    return str(cfg.get("failover", {}).get("dirty_marker_path", DEFAULT_DIRTY_MARKER))


def _singleton():
    global _FAILOVER
    if _FAILOVER is None:
        _FAILOVER = failover_get(_pg_cfg())
    return _FAILOVER


def active_name() -> str | None:
    """Имя активной БД ('primary'/'local'/...) или None если failover не активен."""
    if not failover_active():
        return None
    try:
        db = _singleton().active_db()
        return db.get("name") if db else None
    except Exception as e:  # best-effort
        log.warning("db_access.active_name: %s", e)
        return None


def conn_params() -> dict:
    """Параметры подключения (без connect). failover активен -> DBFailover.conn_params();
    иначе -> одиночный host (PG_CONFIG). Для инструментов, которые сами управляют
    async-connect и statement_timeout (query_postgres_local)."""
    if failover_active():
        return _singleton().conn_params()
    return dict(_pg_cfg())


def get_conn():
    """SYNCHRONOUS psycopg2 connection. Вызывающий закрывает.
    failover активен -> DBFailover.conn_params() (живой primary/local);
    иначе -> одиночный host (текущее поведение tools)."""
    if failover_active():
        return psycopg2.connect(**_singleton().conn_params())
    return psycopg2.connect(**_pg_cfg())


def get_conn_async():
    """ASYNC connection (query_postgres_local использует async_=True + _wait).
    Возвращает conn (НЕ закрыт; вызывающий делает _wait на connect)."""
    if failover_active():
        params = _singleton().conn_params()
    else:
        params = dict(_pg_cfg())
    params.setdefault("connect_timeout", 10)
    return psycopg2.connect(**params, async_=True)


def on_error(exc):
    """В except-обработчиках tools. Best-effort: при connection-ошибке и active!=primary
    DBFailover.on_error может сделать back-switch на primary. НЕ бросает исключений."""
    if not failover_active():
        return None
    try:
        return _singleton().on_error(exc)
    except Exception as e:
        log.warning("db_access.on_error skipped: %s", e)
        return None


def mark_dirty() -> bool:
    """P3: писать dirty-маркер, если мы пишем НЕ в primary (failover активен, active!=primary).
    Вызывают tools при успешной записи с allow_write=True. Idempotent (обновляет ts).
    Возвращает True, если маркер записан/обновлён."""
    if not failover_active():
        return False
    try:
        db = _singleton().active
        if not db or db.get("name") == "primary":
            return False
        path = _marker_path()
        marker = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "active": db.get("name"),
            "host": db.get("host"),
            "port": db.get("port"),
            "by": "db_access.mark_dirty",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False)
            f.write("\n")
        log.info("failover dirty-marker written (active=%s %s:%s)",
                 db.get("name"), db.get("host"), db.get("port"))
        return True
    except Exception as e:
        log.warning("db_access.mark_dirty skipped: %s", e)
        return False
