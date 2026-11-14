from alfa.core.config import get_runtime_settings
"""
Инструмент rag_search — поиск по базе знаний (RAG).

Возвращает структурированную выжимку карточек:
- заголовок
- ключевые факты (summary)
- путь к полному файлу

P1-7 / H4 (rev 1.2, 2026-09-22, OK человека b3388012):
- УБРАНО двойное psycopg2.connect (каждый вызов тек коннекцию).
- УБРАН дублирующий import PG_CONFIG (был try/except с fallback).
- tags-фильтр параметризован (%s::text[]) — больше нет f-string-инъекции.
- finally: conn.close() — коннекция закрывается гарантированно.
"""
import psycopg2
import json
import requests
from typing import List, Optional

# BGE-M3 embedding config (shared with archive_agent)
BGE_M3_URL = "http://127.0.0.1:9090"
BGE_M3_MODEL = "bge-m3"
BGE_EMBEDDING_DIM = 1024


def _get_query_embedding(text: str) -> list:
    """Get embedding for query text from BGE-M3 service."""
    try:
        response = requests.post(
            get_runtime_settings()["embedding_url"] + "/embeddings",
            json={
                "model": get_runtime_settings()["embedding_model"],
                "input": text,
                "encoding_type": "float"
            },
            timeout=30
        )

        if response.status_code != 200:
            print(f"[WARN] embedding failed (status {response.status_code}): {response.text[:300]}")
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

        if isinstance(emb, list) and len(emb) == 1 and isinstance(emb[0], list):
            emb = emb[0]

        if not isinstance(emb, list) or len(emb) != BGE_EMBEDDING_DIM:
            print(f"[WARN] bad embedding (len={len(emb) if isinstance(emb, list) else 'n/a'}, expected {BGE_EMBEDDING_DIM})")
            return None

        return emb
    except Exception as e:
        print(f"[WARN] embedding error: {e}")
        return None


def rag_search(query: str, top_k: int = 3, min_score: float = 0.6, tags: List[str] | None = None) -> str:
    """
    Поиск по базе знаний.

    Args:
        query: Текстовый запрос для поиска
        top_k: Сколько карточек вернуть (не больше 5!)
        min_score: Порог косинусного сходства (0.6-0.7 оптимально)
        tags: Фильтр по тегам (#linux, #waf, #postgresql)

    Returns:
        Структурированная выжимка найденных карточек
    """
    top_k = max(1, min(top_k, 5))

    try:
        from alfa.core.config import PG_CONFIG

        # 1. Генерируем embedding из query
        query_embedding = _get_query_embedding(query)
        if query_embedding is None:
            return (
                "[ERROR] Failed to generate embedding for query. "
                "Check the embedding_url setting in agent_settings.\n"
                "Hint: ensure llama-server was started with --embeddings --pooling cls"
            )

        # 2. Подключаемся к БД (ОДНА коннекция — P1-7)
        conn = psycopg2.connect(**PG_CONFIG)
        try:
            cur = conn.cursor()

            # 3. Формируем tags-фильтр ПАРАМЕТРИЗОВАННО (H4: без f-string-инъекции)
            # Порядок плейсхолдеров: 1) embedding, 2) embedding, 3) min_score,
            # [4) tags если есть], 5) top_k
            params = [json.dumps(query_embedding), json.dumps(query_embedding), min_score]
            if tags:
                tags_clean = [str(t) for t in tags]
                tags_filter = " AND tags @> %s::text[]"
                params.append(tags_clean)
            else:
                tags_filter = ""
            params.append(min(30, top_k * 6))  # c55 964574f2: vector candidates for rerank

            sql = f"""
                SELECT
                    id,
                    title,
                    summary,
                    file_path,
                    tags,
                    1 - (embedding <=> %s::vector) as score
                FROM knowledge_base  -- rest#41 FIX: unqualified (resolves via search_path agents_main,public) == write table
                WHERE embedding IS NOT NULL
                  AND 1 - (embedding <=> %s::vector) >= %s
                  {tags_filter}
                ORDER BY score DESC
                LIMIT %s
            """

            cur.execute(sql, params)
            results = cur.fetchall()
        finally:
            conn.close()

        if not results:
            return "[WARN] Nothing found for the query."

        # RERANK (964574f2 T2, HR f89be775, 13.10 16:29 MSK): bge-reranker-v2-m3
        # (127.0.0.1:8084) переупорядочивает векторный top-candidates -> top_K.
        # Fallback (down/timeout/err) = чистый векторный топ — без ошибок.
        # Switch: agent_settings.reranker.read_rerank (default true).
        _mode = "vector-only"
        _rr_scores = {}
        _rr_pos = {}
        _rr_fn = None
        _RR_DOWN = None
        try:
            from alfa.core.rerank_client import rerank as _rr_fn, RerankUnavailable as _RR_DOWN
        except Exception:
            pass
        if _rr_fn is None:
            results = results[:top_k]
            _mode = "vector-only (no rerank_client)"
        else:
            try:
                _c2conn = psycopg2.connect(**PG_CONFIG)
                try:
                    _c2 = _c2conn.cursor()
                    _c2.execute("SELECT COALESCE(value->>'read_rerank','true') FROM agents_main.agent_settings WHERE setting_key='reranker' AND enabled LIMIT 1")
                    _c2row = _c2.fetchone()
                finally:
                    _c2conn.close()
                _rr_on = (_c2row[0] != "false") if _c2row else True
                if _rr_on and len(results) >= 2:
                    _docs = [(f"{t} {s or ''}").strip()[:1500] for (_i, t, s, _fp, _tg, _sc) in results]
                    _ranked = _rr_fn(query, _docs, top_k=top_k)
                    results = [results[x["index"]] for x in _ranked]
                    _rr_pos = {i: x["score"] for i, x in enumerate(_ranked)}  # i = позиция в выводе (c55 T5 fix: enumerate с 0)
                    _mode = "vector->rerank"
                else:
                    results = results[:top_k]
                    _mode = "vector-only (rr off/1 card)"
            except Exception:
                _mode = "vector-only (reranker down/err)"
                results = results[:top_k]

        # Формируем структурированную выжимку
        output_lines = [f"[INFO] Found {len(results)} cards (mode: {_mode}):\n"]

        for i, (id_, title, summary, file_path, card_tags, score) in enumerate(results, 1):
            output_lines.append(f"\n{i}. [TITLE] {title}")
            output_lines.append(f"   [SCORE] Match: {score:.1%}")
            _rsc = _rr_pos.get(i - 1)
            if _rsc is not None:
                output_lines.append(f"   [RERANK] {_rsc:.2f}")
            if summary:
                output_lines.append(f"   [SUMMARY] {summary[:500]}")
            output_lines.append(f"   [PATH] Full file: {file_path}")
            if card_tags:
                tags_str = ", ".join(tag for tag in card_tags if tag)
                if tags_str:
                    output_lines.append(f"   [TAGS] {tags_str}")
            output_lines.append("")

        return "\n".join(output_lines)

    except Exception as e:
        return f"[ERROR] RAG search error: {str(e)}"


# Metadata
TOOL_NAME = "rag_search"
TOOL_ID = "rag_search"
