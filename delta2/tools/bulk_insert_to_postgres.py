"""Parameterized bulk insert; table and column identifiers are quoted separately."""
import json
import psycopg2
from psycopg2 import sql
from langchain_core.tools import tool
from delta2.core.config import PG_CONFIG
from delta2.core.sql_safety import table_parts
from delta2.core import tg_readonly_guard as _tgro

@tool
def bulk_insert_to_postgres(table_name: str, data: str, columns: str) -> str:
    """Insert a JSON list of objects into table or schema.table. columns is comma-separated."""
    _g = _tgro.guard_write('bulk_insert_to_postgres')
    if _g:
        return _g
    conn = None
    try:
        schema, table = table_parts(table_name)
        names = [name.strip() for name in columns.split(',') if name.strip()]
        rows = json.loads(data)
        if not names or len(names) != len(set(names)) or not isinstance(rows, list) or not rows:
            return 'ERROR: Nonempty unique columns and JSON row list required'
        if not all(isinstance(row, dict) and all(name in row for name in names) for row in rows):
            return 'ERROR: Every row must contain all named columns'
        query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(schema, table),
            sql.SQL(', ').join(map(sql.Identifier, names)), sql.SQL(', ').join(sql.Placeholder() for _ in names))
        conn = psycopg2.connect(**PG_CONFIG)
        with conn.cursor() as cur:
            cur.executemany(query, [tuple(row[name] for name in names) for row in rows])
            count = cur.rowcount
        conn.commit()
        return f'Inserted {count} rows into {table_name}'
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return f'ERROR: {exc}'
    finally:
        if conn is not None:
            conn.close()
