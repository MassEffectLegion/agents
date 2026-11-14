"""
list_postgres_tables — список таблиц БД agents с числом колонок.

H5 (rev 2.0, 2026-09-22, OK человека b3388012): DB_CONFIG -> core/config.py
(PG_CONFIG) — один источник конфигурации БД.
H6: finally: conn.close().
"""
import psycopg2
from langchain_core.tools import tool

from delta1.core.config import PG_CONFIG


@tool
def list_postgres_tables() -> str:
    """List all tables in agent PostgreSQL (127.0.0.1:5432, db=agents) with column counts.
    Useful for discovering the database structure to plan agent tasks."""
    conn = None
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                table_name,
                COALESCE((
                    SELECT COUNT(*) 
                    FROM information_schema.columns c 
                    WHERE c.table_name = t.table_name AND c.table_schema = t.table_schema
                ), 0) as column_count
            FROM information_schema.tables t
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = cursor.fetchall()

        if not tables:
            return "Database is empty. No tables."

        result = ["TABLES IN agents:", "=" * 50]
        for table_name, col_count in tables:
            result.append(f"* {table_name} ({col_count} columns)")
        return "\n".join(result)
    except Exception as e:
        return f"ERROR: {str(e)}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
