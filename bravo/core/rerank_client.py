# -*- coding: utf-8 -*-
"""bravo core: rerank_client — ТОНКИЙ re-export shim (de1f4f4e, rest#18, 2026-11-12).

ЕДИНЫЙ канон: <root>/alfa/tools/rerank_client.py (GO человека de1f4f4e:
«унифицировать DEFAULT на 1.88:8084 + вынос в /tools»). Логика/конфиг/кэш — в каноне;
этот файл только реэкспортирует ПУБЛИЧНЫЕ имена (один экземпляр модуля в процессе).
rag_search.py НЕ меняется: `from bravo.core.rerank_client import rerank,
RerankUnavailable` работает.
"""
import sys as _sys
from pathlib import Path as _Path

# --- путь 1 (первичный): пакетный импорт — единый экземпляр alfa.tools.rerank_client
try:
    from alfa.tools.rerank_client import (
        rerank, RerankUnavailable, search_with_rerank,
        DEFAULT_RERANK_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT, DEFAULT_TOP_N)
except Exception:
    # --- путь 2 (fallback): file-load для layout'ов без пакетного импорта
    import importlib.util as _ilu

    def _find_canon():
        root = _Path(__file__).resolve().parents[2]
        for cand in (root / "alfa" / "tools" / "rerank_client.py",
                     root / "tools" / "rerank_client.py"):
            if cand.exists():
                return cand
        raise ImportError("canon rerank_client.py not found")

    _p = _find_canon()
    _spec = _ilu.spec_from_file_location("alfa_tools_rerank_client", str(_p))
    _mod = _ilu.module_from_spec(_spec)
    _sys.modules.setdefault(_spec.name, _mod)  # замечание #7
    _spec.loader.exec_module(_mod)

    rerank = _mod.rerank
    RerankUnavailable = _mod.RerankUnavailable
    search_with_rerank = _mod.search_with_rerank
    DEFAULT_RERANK_URL = _mod.DEFAULT_RERANK_URL
    DEFAULT_MODEL = _mod.DEFAULT_MODEL
    DEFAULT_TIMEOUT = _mod.DEFAULT_TIMEOUT
    DEFAULT_TOP_N = _mod.DEFAULT_TOP_N

__all__ = ["rerank", "RerankUnavailable", "search_with_rerank",
           "DEFAULT_RERANK_URL", "DEFAULT_MODEL", "DEFAULT_TIMEOUT", "DEFAULT_TOP_N"]
