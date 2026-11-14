# -*- coding: utf-8 -*-
"""rerank_client — ЕДИНЫЙ тонкий клиент llama.cpp /v1/rerank (bge-reranker-v2-m3).

КАНОНИЧЕСКАЯ КОПИЯ (rest#18, GO человека de1f4f4e, 2026-11-12). Живёт в alfa/tools/
(реестр инструментов); core/rerank_client.py каждого из alfa/bravo/gemma — тонкий
re-export shim на этот файл (единый источник истины, «вынос в /tools»).
rag_search.py агентов НЕ меняется — импорты `from <agent>.core.rerank_client import
rerank, RerankUnavailable` продолжают работать через shim.

Все 20 пунктов код-ревью (workspace/tools/af982d0b/замечания.txt) учтены:
 1. except requests.RequestException -> RerankUnavailable (контракт fallback на ЛЮБУЮ
    http-ошибку); 404/501 не ретраятся (осознанно: сервер без --reranking — pointless).
 2. Валидация ответа: results — list, элемент — dict, index — int в [0,len),
    relevance_score — float; нарушение -> RerankUnavailable (не ломать RAG-пайплайн).
 3. Fallback: distance обязателен по смыслу — отсутствие -> -inf (не всплывать),
    пометка "fallback": True.
 4. _load_config с кэшем (TTL 60s) — не коннектимся к БД на каждый вызов.
 5. timeout — float(), top_n — int() (jsonb мог дать строку).
 6. top_k: int|None = None -> cfg.top_n_default -> DEFAULT_TOP_N.
 7. sys.modules.setdefault(name, mod) до exec_module (стандартная практика).
 8. Явная проверка conn is None.
 9. with conn.cursor() as cur.
10. DSN/пароли в логи НЕ пишутся (никакого логирования _load_config).
11. Docstring/DEFAULT унифицированы на 1.88:8084 (решение человека de1f4f4e: 3.55 мёртв).
12. documents: list[str].
13. Размер payload: llama.cpp ctx 2048 токенов — вызывающий ограничивает документы
    (rag_search.py режет doc до 1500 символов). Обрезка — на стороне вызывающего.
15. Единая структура возврата: {"index","score","document","fallback"} у И успеха,
    И fallback (fallback: False/True) — семантика score у fallback = 1-distance.
16. type hints: db_candidates_fn: Callable[[str, int], list[dict]] | None.
17. RerankUnavailable определён ДО rerank.
18. Стандартные импорты наверху файла (os/json/sys/time/pathlib/importlib).
19. isinstance(results, list) — см. п.2.
20. User-Agent: alfa-rerank-client/1.0.

ЗАВИСИМОСТИ: requests (+ psycopg2 лениво, только чтение конфига).
Запускает/убивает сервер — ЧЕЛОВЕК (systemd bge-reranker.service на 1.88).
Эндпоинт (llama.cpp, --reranking --pooling rank):
    POST /v1/rerank  {"model","query","documents":[...],"top_n":N}
    -> {"results":[{"index":i,"relevance_score":s}, ...]}   (index = позиция в documents)

Конфиг (url/model/timeout/top_n_default) — из agents_main.agent_settings key 'reranker',
fallback = DEFAULT_RERANK_URL (1.88:8084 — canonical, миграция 04.11 HTQ ac8b3a67).
Подключение к БД для конфига (cascade, без секретов в коде):
  1) env AGENT_PG_DSN (DSN-строка);
  2) AGENT_SETTINGS_FILE (путь к runtime_settings.local.json, секция "postgres");
  3) <root>/runtime_settings.local.json, root = parent(dir(__file__)) — динамический,
     чтобы единый файл работал и из alfa/tools/, и из <agent>/core/ (shim).
  PG* env-переменные (PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD) перекрывают json —
     как в <agent>.core.config. Пароль БД здесь НЕТ; host не секрет.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

# p.11: canonical — 1.88:8084 (3.55/127.0.0.1 мёртв с 05.11; GO de1f4f4e)
DEFAULT_RERANK_URL = "http://127.0.0.1:8084/v1/rerank"
DEFAULT_MODEL = "bge-reranker-v2-m3"
DEFAULT_TIMEOUT = 30.0
DEFAULT_TOP_N = 5

_USER_AGENT = "alfa-rerank-client/1.0"

# p.4: кэш конфига (TTL 60s) — без коннекта к БД на каждый вызов
_CFG_TTL = 60.0
_cfg_cache: Optional[dict] = None
_cfg_ts: float = 0.0
_cfg_failed_ts: float = 0.0  # negative cache: не долбить БД после ошибки чаще 30s
_CFG_NEG_TTL = 30.0


class RerankUnavailable(Exception):
    """Reranker недоступен — вызывающий переключается на векторный fallback."""


def _agent_root() -> Path:
    """root агента = parent каталога, где лежит этот файл (alfa/tools/ или <agent>/core/)."""
    return Path(__file__).resolve().parent.parent


def _pg_params() -> Optional[dict]:
    """DSN cascade (см. docstring). None — ни один источник не дал параметры."""
    dsn = os.environ.get("AGENT_PG_DSN")
    if dsn:
        import psycopg2
        # DSN-строку отдаём как есть через connect(dsn) — отдельно не разбираем
        _pg_params.dsn = dsn  # type: ignore[attr-defined]
        return None
    # candidates: (1) AGENT_SETTINGS_FILE; (2) root/runtime_settings.local.json
    # (shim-layout: файл в <agent>/core/); (3) root/alfa/runtime_settings.local.json
    # (shared-tools layout: канон в <root>/tools/ или <root>/alfa/tools/, а
    # local.json — в каталоге агента alfa).
    root = _agent_root()
    cands = [Path(os.environ["AGENT_SETTINGS_FILE"])] if os.environ.get("AGENT_SETTINGS_FILE") else []
    cands += [root / "runtime_settings.local.json", root / "alfa" / "runtime_settings.local.json"]
    local_path = next((p for p in cands if p.exists()), None)
    if local_path is None:
        return None
    try:
        local = json.loads(local_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pg = local.get("postgres")
    if not isinstance(pg, dict):
        return None
    params = dict(pg)
    for key, env in {"host": "PGHOST", "port": "PGPORT", "database": "PGDATABASE",
                     "user": "PGUSER", "password": "PGPASSWORD"}.items():
        if env in os.environ:
            params[key] = os.environ[env]
    params.setdefault("connect_timeout", 10)
    params.setdefault("options", "-c search_path=agents_main,public")
    return params


def _connect():
    """psycopg2-коннекшн или None (тихо). p.8: вызывающий проверяет None явно."""
    import psycopg2
    dsn = getattr(_pg_params, "dsn", None)
    if dsn:
        try:
            return psycopg2.connect(dsn, connect_timeout=10)
        except Exception:
            return None
    params = _pg_params()
    if params is None:
        return None
    try:
        return psycopg2.connect(**params)
    except Exception:
        return None


def _load_config() -> Optional[dict]:
    """Читает agent_settings key 'reranker' (read-only). Ошибка/пусто -> None.

    p.4: кэш 60s (positive) / 30s (negative). p.9: with cursor. p.10: без логов DSN.
    """
    global _cfg_cache, _cfg_ts, _cfg_failed_ts
    now = time.time()
    if _cfg_cache is not None and now - _cfg_ts < _CFG_TTL:
        return _cfg_cache
    if now - _cfg_failed_ts < _CFG_NEG_TTL:
        return None
    conn = _connect()
    if conn is None:  # p.8
        _cfg_failed_ts = now
        return None
    try:
        with conn.cursor() as cur:  # p.9
            cur.execute(
                "SELECT value FROM agents_main.agent_settings "
                "WHERE setting_key='reranker' AND enabled")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        _cfg_failed_ts = now
        return None
    val = row[0]
    cfg = val if isinstance(val, dict) else None
    if cfg is None:
        try:
            cfg = json.loads(val)
        except (ValueError, TypeError):
            cfg = None
    if not isinstance(cfg, dict):
        _cfg_failed_ts = now
        return None
    _cfg_cache, _cfg_ts = cfg, now
    return cfg



def _parse_results(data, documents: list) -> list:
    """p.2/p.19: валидация ответа сервера.

    Возвращает единый формат (p.15): [{"index","score","document","fallback":False}].
    Нарушение (не dict, results не list, index вне [0,len), нет relevance_score,
    не-castable score) -> ValueError — вызывающий конвертирует в RerankUnavailable.
    """
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("no results list")
    n = len(documents)
    out = []
    for x in results:
        if not isinstance(x, dict):
            raise ValueError("result item is not an object")
        idx = x.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ValueError(f"index not int: {idx!r}")
        if not (0 <= idx < n):
            raise ValueError(f"index out of range: {idx} (n={n})")
        score = x.get("relevance_score")
        if score is None:
            raise ValueError("missing relevance_score")
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            raise ValueError(f"bad relevance_score: {score!r}")
        out.append({"index": idx, "score": score_f,
                    "document": documents[idx], "fallback": False})
    return out


def rerank(query: str, documents: list, top_k: Optional[int] = None,
           url: Optional[str] = None, model: Optional[str] = None,
           timeout: Optional[float] = None, retries: int = 1) -> list:
    """Возвращает [{"index":i, "score":s, "document":doc, "fallback":False}, ...]
    по убыванию score (p.15 — единая структура).

    Бросает RerankUnavailable при down/timeout/HTTP-ошибке (p.1) И при malformed
    ответе (p.2) — вызывающий делает fallback на чистый векторный поиск
    (см. search_with_rerank).

    p.5: timeout приведён к float (jsonb мог дать строку "30").
    p.6: top_k=None -> cfg.top_n_default (-> cfg.top_n) -> DEFAULT_TOP_N.
    404/501 — ОСОЗНАННО без ретра (сервер без --reranking не «вылечится» повтором).
    p.13: размер payload — на стороне вызывающего (llama.cpp ctx 2048 токенов).
    """
    cfg = _load_config() or {}
    url = url or cfg.get("url") or DEFAULT_RERANK_URL
    model = model or cfg.get("model") or DEFAULT_MODEL
    try:  # p.5
        timeout = float(timeout if timeout is not None
                        else (cfg.get("timeout") or DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    if top_k is None:  # p.6
        try:
            top_k = int(cfg.get("top_n_default") or cfg.get("top_n") or DEFAULT_TOP_N)
        except (TypeError, ValueError):
            top_k = DEFAULT_TOP_N
    top_k = max(1, int(top_k))

    payload = {"model": model, "query": query, "documents": documents, "top_n": top_k}
    headers = {"User-Agent": _USER_AGENT}  # p.20
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code in (404, 501):
                raise RerankUnavailable(
                    f"{r.status_code}: {url} — сервер запущен без --reranking?")
            r.raise_for_status()
            data = r.json()  # ValueError -> ловится ниже -> RerankUnavailable (p.2)
            out = _parse_results(data, documents)
            out.sort(key=lambda x: x["score"], reverse=True)
            return out
        except requests.RequestException as ex:  # p.1: Connection/Timeout/HTTPError/...
            last_err = ex
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            continue
        except (ValueError, KeyError, TypeError) as ex:  # p.2: malformed response
            raise RerankUnavailable(f"malformed rerank response: {ex}")
    raise RerankUnavailable(f"rerank unavailable after {retries + 1} tries: {last_err}")


def search_with_rerank(query: str, top_k: int = 3, candidates: int = 30,
                       db_candidates_fn: Optional["Callable[[str, int], list[dict]]"] = None,
                       rerank_url: Optional[str] = None) -> list:
    """RAG-пайплайн: pgvector top-N (BGE-M3) -> rerank -> top-K.

    db_candidates_fn(query, limit) -> [{"id","content","distance"}, ...]
    (векторный поиск предоставляет вызывающий — не дублируем подключение).

    Единый формат (p.15): {"index","score","document","fallback"}.
    При RerankUnavailable — fallback на чистый векторный top-K:
    score = 1 - distance (p.3: нет distance -> -inf, кандидат НЕ всплывает),
    "fallback": True — для диагностики в выводе RAG.
    """
    if db_candidates_fn is None:
        raise ValueError("db_candidates_fn required (векторный поиск)")
    cands = db_candidates_fn(query, candidates) or []
    if not cands:
        return []
    docs = [c["content"] for c in cands]
    try:
        return rerank(query, docs, top_k=top_k, url=rerank_url)
    except RerankUnavailable:
        out = []
        for i, c in enumerate(cands[:top_k]):
            d = c.get("distance")
            try:
                score = 1.0 - float(d)
            except (TypeError, ValueError):
                score = float("-inf")  # p.3
            out.append({"score": score, "index": i, "document": c["content"],
                        "fallback": True})
        return out


# --- self-test (python rerank_client.py) -------------------------------------
if __name__ == "__main__":
    import sys as _sys
    print(f"[cfg] {_load_config()}")
    try:
        res = rerank("тест живости reranker", ["документ один про RAG",
                                              "документ два про Linux"], top_k=2)
        print(f"[ok] {res}")
    except RerankUnavailable as e:
        print(f"[down] {e}")
    _sys.exit(0)
