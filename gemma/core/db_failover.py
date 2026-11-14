# alfa — core/db_failover.py (DRAFT, rest#82) — НЕ интегрирован, НЕ активен.
# 0a319d0b follow-up: failover БД primary(1.77) -> reserve(3.55) -> local(127.0.0.1).
# Активируется ТОЛЬКО после GO человека + добавления "postgres.databases[]" в runtime_settings.
# Сейчас runtime_settings.local.json содержит старый одиночный postgres.host — этот модуль
# лежит "спящим": его import не меняет поведение, пока конфиг не расширен и tools не поправлены.
#
# ЦЕЛЬ: агент на любой ВМ (1.77/3.55) продолжает работать при потере первичной БД
# (сценарий 220-разрыва из 0a319d0b). sticky-режим: не прыгаем между БД в каждом запросе.

import socket, time, json, logging

log = logging.getLogger("db_failover")

# --- Конфиг (читаем из runtime_settings.local.json, key postgres.databases) ---
# Ожидается структура:
# "postgres": {
#   "databases": [
#     {"name":"primary","host":"127.0.0.1","port":5432,"priority":1},
#     {"name":"reserve","host":"127.0.0.1","port":5432,"priority":2},
#     {"name":"local",  "host":"127.0.0.1",   "port":5432,"priority":3}
#   ],
#   "database":"agents","user":"agents","password": "CHANGE_ME",
#   "options":"-c search_path=agents_main,public -c statement_timeout=60000 -c lock_timeout=10000 -c timezone=Europe/Moscow",
#   "failover":{"enabled":true,"probe_timeout_ms":2500,"reprobe_interval_s":30,"sticky":true}
# }

class DBFailover:
    def __init__(self, pg_cfg: dict):
        self.cfg = pg_cfg
        self.dbs = pg_cfg.get("databases", None)
        self.failover = pg_cfg.get("failover", {})
        self.enabled = bool(self.dbs) and self.failover.get("enabled", False)
        self.probe_timeout = self.failover.get("probe_timeout_ms", 2500) / 1000.0
        self.reprobe_s = self.failover.get("reprobe_interval_s", 30)
        self.sticky = self.failover.get("sticky", True)
        self.active = None          # active db dict
        self._last_reprobe = 0.0
        self._order = sorted(self.dbs, key=lambda d: d.get("priority", 99)) if self.dbs else []

    # --- TCP + SELECT 1 probe ---
    def _probe(self, db: dict) -> bool:
        try:
            with socket.create_connection((db["host"], int(db["port"])), timeout=self.probe_timeout):
                pass
        except Exception as e:
            log.warning("probe %s:%s TCP fail: %s", db["host"], db["port"], e)
            return False
        # SELECT 1 via psycopg (connect_timeout в options)
        try:
            import psycopg2
            opts = self.cfg.get("options", "")
            conn = psycopg2.connect(
                host=db["host"], port=int(db["port"]),
                dbname=self.cfg.get("database","agents"),
                user=self.cfg.get("user","agents"),
                password=self.cfg.get("password", "CHANGE_ME"),
                connect_timeout=3, options=opts,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.close()
            return True
        except Exception as e:
            log.warning("probe %s:%s SELECT 1 fail: %s", db["host"], db["port"], e)
            return False

    # --- pick active (по priority, первый OK); sticky держит текущую, пока жива ---
    def active_db(self) -> dict:
        if not self.enabled:
            # старый режим: одиночный host (обратно-совместимо)
            return {
                "name": "primary",
                "host": self.cfg.get("host"),
                "port": self.cfg.get("port", 5432),
            }
        now = time.time()
        # sticky: если активная ещё жива и не прошёл reprobe — держим
        if self.sticky and self.active and (now - self._last_reprobe) < self.reprobe_s:
            return self.active
        # инициализация / reprobe: пробуем по priority, НО primary всегда предпочтительна
        # (при возврате primary — back-switch, т.к. primary = источник истины)
        for db in self._order:
            if self._probe(db):
                if self.active is None or db.get("name") != self.active.get("name"):
                    log.info("DB active -> %s (%s:%s)", db["name"], db["host"], db["port"])
                    self._notify_failover(self.active, db)
                self.active = db
                self._last_reprobe = now
                return db
        # ни одна не жива: держим последнюю известную (пусть tool сам ругнётся)
        log.error("DB failover: НЕТ живой БД из списка %s", [d['name'] for d in self._order])
        return self.active or self._order[0]

    # --- on SQL runtime error: если мы НЕ на primary и ошибка connection — reprobe primary ---
    def on_error(self, exc) -> dict:
        if not self.enabled or self.active is None:
            return self.active
        name = type(exc).__name__
        is_conn = name in ("OperationalError","InterfaceError","DatabaseError") or \
                  "could not connect" in str(exc).lower() or "connection" in str(exc).lower()
        if is_conn and self.active.get("name") != "primary":
            for db in self._order:
                if db.get("name") == "primary" and self._probe(db):
                    log.info("DB failover: back-switch -> primary (1.77 вернулась)")
                    self._notify_failover(self.active, db)
                    self.active = db
                    self._last_reprobe = time.time()
                    return db
        return self.active

    def _notify_failover(self, from_db, to_db):
        # 1 строка в log + agent_coordination (best-effort, не бросает исключений)
        try:
            import psycopg2
            db = to_db or self.active
            conn = psycopg2.connect(host=db["host"], port=int(db["port"]),
                dbname=self.cfg.get("database","agents"), user=self.cfg.get("user","agents"),
                password=self.cfg.get("password", "CHANGE_ME"), connect_timeout=3,
                options=self.cfg.get("options",""))
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_coordination (topic, agent, action, payload) VALUES "
                    "('db_failover','gemma','switch', %s)",
                    (json.dumps({"from": (from_db or {}).get("name"),
                                 "to": to_db.get("name"),
                                 "host": to_db.get("host")}, ensure_ascii=False),))
            conn.commit(); conn.close()
        except Exception as e:
            log.warning("db_failover notify skipped: %s", e)

    # --- conn_params для SQL-инструментов (возвращает dict для psycopg.connect) ---
    def conn_params(self) -> dict:
        db = self.active_db()
        return dict(
            host=db["host"], port=int(db["port"]),
            dbname=self.cfg.get("database","agents"),
            user=self.cfg.get("user","agents"),
            password=self.cfg.get("password", "CHANGE_ME"),
            connect_timeout=5, options=self.cfg.get("options",""),
        )

# --- Singleton-доступ для tools (lazy) ---
_FAILOVER = None
def get(pg_cfg: dict) -> DBFailover:
    global _FAILOVER
    if _FAILOVER is None:
        _FAILOVER = DBFailover(pg_cfg)
    return _FAILOVER
