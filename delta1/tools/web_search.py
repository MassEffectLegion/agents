# -*- coding: utf-8 -*-
"""web_search — поиск в интернете по agent_settings key 'internet_search' (task 4c56d0a4).
ТОЛЬКО провайдеры, объявленные в БД (duckduckgo_lite, github_api). curl-любой-URL НЕ даём.
"""
import html as _html
import json
import re
import urllib.parse
import urllib.request

from langchain_core.tools import tool

# UA: из agent_settings internet_search.user_agent, fallback браузерный.
# ВАЖНО: ddg-lite отдаёт anomaly-страницу на "ботовские" UA (проверено 26.09 21:5x MSK).
_UA_FALLBACK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _load_cfg() -> dict:
    from bravo.core.config import PG_CONFIG
    import psycopg2
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM agents_main.agent_settings WHERE setting_key='internet_search' AND enabled IS NOT FALSE")
        row = cur.fetchone()
        return row[0] if row and isinstance(row[0], dict) else {}
    finally:
        conn.close()


def _http_get(url: str, timeout: int, ua: str = "") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua or _UA_FALLBACK, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ddg_lite(query: str, cfg: dict, max_results: int) -> list:
    provider = next((p for p in cfg.get("providers", []) if p.get("name") == "duckduckgo_lite"), None)
    if not provider:
        return []
    url = provider["url"].replace("{query}", urllib.parse.quote_plus(query))
    page = _http_get(url, cfg.get("timeout_sec", 15), ua=cfg.get("user_agent") or _UA_FALLBACK)
    if "anomaly.js" in page[:2000]:
        return []  # ddg bot-check: честно пустой список, не ошибка
    results = []
    # result__a = заголовок+ссылка, result__snippet = сниппет
    links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for i, (href, title) in enumerate(links[:max_results]):
        # uddg-redirect decode
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        title = re.sub(r"<[^>]+>", "", title)
        snippet = re.sub(r"<[^>]+>", "", snippets[i]) if i < len(snippets) else ""
        results.append({"title": _html.unescape(title).strip(), "url": href, "snippet": _html.unescape(snippet).strip()[:300]})
    return results


def _github(query: str, cfg: dict, max_results: int) -> list:
    provider = next((p for p in cfg.get("providers", []) if p.get("name") == "github_api"), None)
    if not provider:
        return []
    url = provider["url"].replace("{query}", urllib.parse.quote_plus(query))
    data = json.loads(_http_get(url, cfg.get("timeout_sec", 15)))
    return [
        {"title": it.get("full_name"), "url": it.get("html_url"),
         "snippet": (it.get("description") or "")[:300] + f" (stars: {it.get('stargazers_count', 0)})"}
        for it in data.get("items", [])[:max_results]
    ]


@tool
def web_search(query: str, provider: str = "duckduckgo_lite", max_results: int = 5) -> str:
    """
    Поиск актуальной информации в интернете (библиотеки, CVE, конфиги, документация).

    Args:
        query: поисковый запрос
        provider: 'duckduckgo_lite' (обычный веб-поиск) или 'github_api' (поиск по GitHub-репозиториям)
        max_results: сколько результатов вернуть (1-10, по умолчанию 5)

    Доступны ТОЛЬКО провайдеры из agent_settings key 'internet_search'.
    """
    cfg = _load_cfg()
    if not cfg or not cfg.get("enabled", True):
        return "ERROR: agent_settings key 'internet_search' не найден/выключен."
    if provider not in {p.get("name") for p in cfg.get("providers", [])}:
        return f"ERROR: провайдер '{provider}' не разрешён. Разрешены: {sorted({p.get('name') for p in cfg.get('providers', [])})}."
    max_results = max(1, min(10, int(max_results)))
    try:
        if provider == "github_api":
            results = _github(query, cfg, max_results)
        else:
            results = _ddg_lite(query, cfg, max_results)
        if not results:
            return f"Нет результатов по запросу '{query}' (provider={provider})."
        out = []
        for i, r in enumerate(results, 1):
            out.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
        return "\n".join(out)
    except Exception as e:
        return f"ERROR web_search: {type(e).__name__}: {str(e)[:200]}"
