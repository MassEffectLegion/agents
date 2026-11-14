"""Schema-qualified PostgreSQL table inspection with quoted identifiers."""
import json
import psycopg2
from psycopg2 import sql
from langchain_core.tools import tool
from bravo.core.config import PG_CONFIG
from bravo.core.sql_safety import table_parts

@tool
def get_table_schema(table_name: str) -> str:
    """Return column definitions and three sample rows. Accept table or schema.table."""
    conn = None
    try:
        schema, table = table_parts(table_name)
        conn = psycopg2.connect(**PG_CONFIG)
        with conn.cursor() as cur:
            cur.execute('SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (schema, table))
            columns = cur.fetchall()
            if not columns:
                return f'Table not found: {table_name}'
            cur.execute(sql.SQL('SELECT * FROM {} LIMIT 3').format(sql.Identifier(schema, table)))
            return json.dumps({'table': table_name, 'columns': columns, 'samples': cur.fetchall()}, ensure_ascii=False, default=str)
    except Exception as exc:
        return f'ERROR: {exc}'
    finally:
        if conn is not None:
            conn.close()
