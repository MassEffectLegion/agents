# -*- coding: utf-8 -*-
"""delta1 core: rerank_client — тонкий клиент llama.cpp /v1/rerank (bge-reranker-v2-m3).

(Копия bravo core/rerank_client.py, адаптирована под delta1 — import delta1.core.config.
Создано bravo c55 (964574f2, Т2) по явному указанию человека в HR f89be775:
"добавь логику обработки операции при чтении RAG карточек через rerank у себя и у delta1".)

ЗАВИСИМОСТИ: только requests. Запускает/убивает сервер — ЧЕЛОВЕК (bat run_reranker_v2.bat).
Эндпоинт (llama.cpp, --reranking + --pooling rank):
    POST /v1/rerank  {"model","query","documents":[...],"top_n":N}
    -> {"results":[{"index":i,"relevance_score":s}, ...]}   (index = позиция в documents)

Конфиг (url/model/timeout/top_n) — из agent_settings key 'reranker' (jsonb),
fallback = DEFAULT_RERANK_URL (решение человека 13.10: 127.0.0.1:8084, ПРАСТОЯЩАЯ служба).
Пароль БД здесь НЕТ; host не секрет.
"""
from __future__ import annotations
import json
import time
import requests

DEFAULT_RERANK_URL = "http://127.0.0.1:8084/v1/rerank"
DEFAULT_MODEL = "bge-reranker-v2-m3"
DEFAULT_TIMEOUT = 30


def _load_config():
    """Читает agent_settings key 'reranker' (read-only). Ошибка -> None (fallback)."""
    try:
        import psycopg2
        from pathlib import Path
        import os
        cfg = os.environ.get("AGENT_PG_DSN")
        dsn = cfg or None
        if not dsn:
            p = Path(__file__).resolve().parents[1] / "config.py"
            ns = {}
            exec(p.read_text(encoding="utf-8"), ns)
            dsn = ns.get("PG_DSN") or ns.get("DB_DSN")
        conn = psycopg2.connect(dsn, connect_timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM agents_main.agent_settings "
                "WHERE setting_key='reranker' AND enabled"
            )
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def rerank(query: str, documents: list, top_k: int = 5,
           url: str | None = None, model: str | None = None,
           timeout: float | None = None, retries: int = 1) -> list:
    """Возвращает [{"index":i, "score":s, "document":doc}, ...] по убыванию score.

    Бросает RerankUnavailable при down/timeout (после retries) — вызывающий делает fallback
    на чистый векторный поиск (см. search_with_rerank).
    """
    cfg = _load_config() or {}
    url = url or cfg.get("url") or DEFAULT_RERANK_URL
    model = model or cfg.get("model") or DEFAULT_MODEL
    timeout = timeout or cfg.get("timeout") or DEFAULT_TIMEOUT

    payload = {"model": model, "query": query, "documents": documents, "top_n": top_k}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code in (404, 501):
                raise RerankUnavailable(f"{r.status_code}: {url} — сервер запущен без --reranking?")
            r.raise_for_status()
            data = r.json()
            out = [{"index": x["index"], "score": float(x["relevance_score"]),
                    "document": documents[x["index"]]}
                   for x in data.get("results", [])]
            out.sort(key=lambda x: x["score"], reverse=True)
            return out
        except (requests.ConnectionError, requests.Timeout) as ex:
            last_err = ex
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            continue
    raise RerankUnavailable(f"rerank unavailable after {retries+1} tries: {last_err}")


class RerankUnavailable(Exception):
    """Reranker недоступен — вызывающий переключается на векторный fallback."""


def search_with_rerank(query: str, top_k: int = 3, candidates: int = 30,
                       db_candidates_fn=None, rerank_url: str | None = None):
    """RAG-пайплайн: pgvector top-N (BGE-M3 :9090) -> rerank -> top-K.

    db_candidates_fn(query, limit) -> [{"id","content","distance"}, ...]. При RerankUnavailable —
    fallback на чистый векторный top-K (score = 1 - distance).
    """
    if db_candidates_fn is None:
        raise ValueError("db_candidates_fn required (векторный поиск)")
    cands = db_candidates_fn(query, candidates) or []
    if not cands:
        return []
    docs = [c["content"] for c in cands]
    try:
        rr = rerank(query, docs, top_k=top_k, url=rerank_url)
        return rr
    except RerankUnavailable:
        return [{"score": 1.0 - c.get("distance", 0.0), "index": i, "document": c["content"]}
                for i, c in enumerate(cands[:top_k])]
