"""Idempotent provisioning of runtime configuration in existing agent_settings."""
import json
import psycopg2
from .config import PG_CONFIG, AGENT_ROOT, LONGLIVE_AGENT_NAME

def seed_runtime_settings():
    defaults = json.loads((AGENT_ROOT / 'runtime_defaults.json').read_text(encoding='utf-8'))
    common = {key:value for key,value in defaults.items() if key != 'llama_server_url'}
    own = {'llama_server_url': defaults['llama_server_url']}
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS agents_main.agent_settings (
                setting_key TEXT PRIMARY KEY, category TEXT, value JSONB,
                enabled BOOLEAN DEFAULT TRUE, description TEXT, updated_by TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW())""")
            for key,value,description in (
                ('longlive.common', common, 'Shared runtime defaults for delta2/bravo; per-agent override wins'),
                ('longlive.'+LONGLIVE_AGENT_NAME, own, 'Runtime overrides for '+LONGLIVE_AGENT_NAME),
            ):
                cur.execute("""INSERT INTO agents_main.agent_settings
                    (setting_key,category,value,enabled,description,updated_by,updated_at)
                    VALUES (%s,'longlive',%s::jsonb,TRUE,%s,'code_repair',NOW())
                    ON CONFLICT (setting_key) DO NOTHING""", (key,json.dumps(value),description))
            # Preserve an existing ASOP setting. Import the previous storage only if missing.
            cur.execute("SELECT 1 FROM agents_main.agent_settings WHERE setting_key='asop_config'")
            if cur.fetchone() is None:
                asop = None
                cur.execute("SELECT to_regclass('agents_main.agent_runtime_settings')")
                if cur.fetchone()[0]:
                    cur.execute('SELECT settings FROM agents_main.agent_runtime_settings WHERE agent=%s', (LONGLIVE_AGENT_NAME,))
                    row=cur.fetchone()
                    if row:
                        asop=row[0].get('asop')
                local_path=AGENT_ROOT/'runtime_settings.local.json'
                if not asop and local_path.exists():
                    asop=json.loads(local_path.read_text(encoding='utf-8')).get('asop')
                if asop:
                    cur.execute("""INSERT INTO agents_main.agent_settings
                        (setting_key,category,value,enabled,description,updated_by,updated_at)
                        VALUES ('asop_config','credentials',%s::jsonb,TRUE,'ASOP connection settings','code_repair',NOW())
                        ON CONFLICT (setting_key) DO NOTHING""", (json.dumps(asop),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
