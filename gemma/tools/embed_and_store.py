from gemma.core.config import get_runtime_settings
import json
import os
import requests
import psycopg2
from langchain_core.tools import tool

# BGE-M3 embedding config
BGE_M3_URL = "http://127.0.0.1:9090"  # c467: CANONICAL BGE (agent_settings.bge)
BGE_M3_MODEL = "bge-m3"
BGE_EMBEDDING_DIM = 1024
_schema_ready = False
MAX_EMBED_CHARS = 8000  # max chars for single embedding (BGE-M3 ~8192 tokens)

from gemma.core.config import PG_CONFIG as DB_CONFIG
from gemma.tools.path_guard import require_allowed
from gemma.core import tg_readonly_guard as _tgro


def _vector_literal(embedding: list) -> str:
    """Convert list of floats to pgvector text literal '[a,b,c]'."""
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


def _get_embedding(text: str) -> list:
    """Get embedding from BGE-M3 via llama-server for a single text."""
    try:
        print(f"INFO: getting embedding for text (len={len(text)} chars)")
        response = requests.post(
            get_runtime_settings()["embedding_url"] + "/embeddings",
            json={
                "model": get_runtime_settings()["embedding_model"],
                "input": text,
                "encoding_type": "float"
            },
            timeout=120
        )
        if response.status_code != 200:
            print(f"WARNING: embedding failed (status {response.status_code}): {response.text[:300]}")
            return None

        data = response.json()
        emb = None
        
        if isinstance(data, list):
            if data and isinstance(data[0], dict) and "embedding" in data[0]:
                emb = data[0]["embedding"]
        elif isinstance(data, dict):
            if "data" in data and isinstance(data["data"], list) and data["data"]:
                item = data["data"][0]
                if isinstance(item, dict) and "embedding" in item:
                    emb = item["embedding"]
            if emb is None and "embedding" in data:
                emb = data["embedding"]
        
        if emb is None:
            print(f"WARNING: unexpected response type={type(data).__name__}: {str(data)[:200]}")
            return None
        
        # Unwrap nested array: [[...1024...]] -> [...1024...]
        if isinstance(emb, list) and len(emb) == 1 and isinstance(emb[0], list):
            emb = emb[0]
        
        if not isinstance(emb, list) or len(emb) != BGE_EMBEDDING_DIM:
            print(f"WARNING: bad embedding (len={len(emb) if isinstance(emb, list) else 'n/a'}, expected {BGE_EMBEDDING_DIM})")
            return None

        return emb
    except Exception as e:
        print(f"WARNING: embedding error: {e}")
        return None


@tool
def embed_and_store(file_path: str, task_id: str, agents: str | None = None,
                    title: str | None = None, tags: str | None = None, summary: str | None = None) -> str:
    """Create embedding from a knowledge card markdown file and store ONE vector record in PostgreSQL knowledge_base table.

    This tool:
    1. Reads a .md knowledge card file
    2. Uses the FULL file content (truncated to ~8000 chars if needed) as a single record
    3. Sends full content to BGE-M3 embedding model (llama-server on port 9090)
    4. Stores ONE vector record in the knowledge_base table with pgvector

    Args:
        file_path: Path to the markdown knowledge card file (e.g., knowledge_base/task_xxx_knowledge.md)
        task_id: Source task UUID (links to tasks.task_id)
        agents: Agent name(s) that performed the task (e.g., 'archive_agent, devops_agent')
        title: Short title of the knowledge card (defaults to filename)
        tags: Comma-separated tags (e.g., '#linux #redos #nginx')
        summary: Short summary of the knowledge card
    """

    _g = _tgro.guard_write('embed_and_store')
    if _g:
        return _g

    # --- 1. Read the markdown file ---
    try:
        file_path = require_allowed(file_path)
        if not os.path.exists(file_path):
            return f"ERROR: File not found: {file_path}"
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"ERROR: Failed to read file: {str(e)}"

    # --- 2. Defaults ---
    if title is None:
        title = os.path.basename(file_path).replace('.md', '')
    if tags is None:
        tags = ""
    if summary is None:
        lines = content.split('\n')
        summary_lines = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('**') and len(line) > 20:
                summary_lines.append(line)
        summary = " ".join(summary_lines[:2]) if summary_lines else content[:200]

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # --- 3. Use full content as single record (no chunking) ---
    # Truncate to max embedding length if needed
    store_text = content if len(content) <= MAX_EMBED_CHARS else content[:MAX_EMBED_CHARS]
    print(f"INFO: Using full content ({len(content)} chars, storing {len(store_text)} chars)")

    # --- 4. Get embedding for full content ---
    print(f"INFO: Getting embedding from BGE-M3 at {get_runtime_settings()['embedding_url']}...")
    embedding = _get_embedding(store_text)

    if embedding is None:
        return (
            "ERROR: No embedding generated. Check BGE-M3 service.\n"
            f"  URL: {get_runtime_settings()['embedding_url']}\n"
            "  Hint: verify llama-server was started with --embeddings "
            "and --pooling cls; try `curl {url}/health`."
        )

    # --- 5. Store in PostgreSQL ---
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        global _schema_ready
        if not _schema_ready:
            # make sure pgvector extension is present
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # ensure parent table exists (FK target); safe no-op if already there
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY
                )
            """)

            # ensure knowledge_base exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_task_id TEXT REFERENCES tasks(task_id),
                    agents VARCHAR(50) NULL,
                    title VARCHAR(255) NOT NULL,
                    file_path VARCHAR(500) NOT NULL,
                    tags TEXT[],
                    summary TEXT,
                    embedding vector(1024),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # GIN-индекс по тегам
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_knowledge_tags
                ON knowledge_base USING GIN (tags)
            """)

            # Векторный индекс (HNSW)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
                ON knowledge_base USING hnsw (embedding vector_cosine_ops)
            """)
            conn.commit()
            _schema_ready = True
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (task_id + ':' + file_path,))
        # --- 6. Check for duplicate records before inserting ---
        cursor.execute("""
            SELECT id FROM knowledge_base
            WHERE source_task_id = %s AND file_path = %s
            LIMIT 1
        """, (task_id, file_path))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""UPDATE knowledge_base SET agents=%s, title=%s, tags=%s,
                summary=%s, embedding=%s::vector WHERE id=%s""",
                (agents, title, tag_list, summary, _vector_literal(embedding), existing[0]))
            conn.commit()
            return f'Updated knowledge card {existing[0]}'
        # --- 6. Insert SINGLE record for the entire file ---
        cursor.execute("""
            INSERT INTO knowledge_base
                (source_task_id, agents, title, file_path, tags, summary, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector) RETURNING id
        """, (
            task_id,
            agents,
            title,
            file_path,
            tag_list,
            summary,
            _vector_literal(embedding),
        ))

        record_id = cursor.fetchone()[0]
        conn.commit()

        result = (
            f"Stored 1 record in knowledge_base from {file_path}. ID: {record_id}\n"
            f"Task ID: {task_id}, Title: {title}, Tags: {tags}\n"
            f"Content length: {len(store_text)} chars, Embedding dimension: {len(embedding)}"
        )
        return result

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return f"ERROR: Failed to store in PostgreSQL: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
