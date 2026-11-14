# -*- coding: utf-8 -*-
"""ask_vision — vision-модель по agent_settings key 'vision' (task 4c56d0a4).
live_check=true: проверяет /v1/models (кэш 60с); DOWN -> честная ошибка, без галлюцинаций.
"""
import base64
import json
import time
import urllib.request

from langchain_core.tools import tool

_check_cache = {"ts": 0.0, "ok": False, "models": [], "err": ""}
CACHE_TTL = 60  # сек


def _load_vision_cfg() -> dict:
    from bravo.core.config import PG_CONFIG
    import psycopg2
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM agents_main.agent_settings WHERE setting_key='vision' AND enabled IS NOT FALSE")
        row = cur.fetchone()
        return row[0] if row and isinstance(row[0], dict) else {}
    finally:
        conn.close()


def _live_check(cfg: dict) -> dict:
    now = time.monotonic()
    if now - _check_cache["ts"] < CACHE_TTL:
        return _check_cache
    url = (cfg.get("url") or "http://127.0.0.1:8181/v1").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/models", timeout=cfg.get("check_timeout_sec", 10)) as r:
            data = json.loads(r.read().decode())
        models = [m.get("id") for m in data.get("data", [])]
        res = {"ts": now, "ok": True, "models": models, "err": ""}
    except Exception as e:
        res = {"ts": now, "ok": False, "models": [], "err": str(e)[:150]}
    _check_cache.clear()
    _check_cache.update(res)
    return res


@tool
def ask_vision(image_path: str, question: str) -> str:
    """
    Показать картинку vision-модели (OpenAI-compatible, из agent_settings key 'vision').

    Args:
        image_path: локальный путь к файлу картинки (png/jpg/webp)
        question: вопрос к модели о картинке

    Если vision-модель DOWN — вернёт ЧЕСТНУЮ ошибку с адресом и рекомендацией,
    а не выдуманный ответ.
    """
    cfg = _load_vision_cfg()
    if not cfg:
        return "ERROR: agent_settings key 'vision' не найден/выключен."
    check = _live_check(cfg)
    url = (cfg.get("url") or "").rstrip("/")
    if not check["ok"]:
        return (
            f"VISION UNAVAILABLE: {cfg.get('host') or 'host'}:{cfg.get('port') or '?'} — {check['err']}. "
            f"Модель не запущена (или порт занят). Уточните у человека: запустить vision-модель на "
            f"{cfg.get('url')}? НЕ пытайтесь описать картинку сами."
        )
    try:
        with open(image_path, "rb") as f:
            img = f.read()
        if len(img) > 8 * 1024 * 1024:
            return f"ERROR: картинка слишком большая ({len(img)} bytes > 8MB)."
        b64 = base64.b64encode(img).decode()
        ext = image_path.rsplit(".", 1)[-1].lower() if "." in image_path else "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "bmp": "bmp"}.get(ext, "png")
        body = json.dumps({
            "model": cfg.get("model"),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                ],
            }],
            "max_tokens": 1024,
        }).encode()
        req = urllib.request.Request(
            url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
        with urllib.request.urlopen(req, timeout=cfg.get("check_timeout_sec", 10) * 30) as r:
            data = json.loads(r.read().decode())
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return f"VISION ({cfg.get('model')}): {content}"
    except Exception as e:
        return f"ERROR ask_vision: {type(e).__name__}: {str(e)[:200]}"
