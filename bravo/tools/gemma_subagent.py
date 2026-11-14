"""tools/gemma_subagent.py — саб-агент на gemma (127.0.0.1:8181, gemma-4-E4B-it-Q4_0, 32k ctx).

Задача 63613c06 (человек): использовать рядом-запущенную LLM gemma как саб-агента
для одноразовых задач — экономия основного контекста bravo.

НАЗНАЧЕНИЕ (только механика, НЕ аналитика/решения):
  - суммаризация длинных tool-выводов/логов (сырьё не тащим в основную LLM),
  - извлечение фактов из больших JSON/текстов,
  - парсинг/структурирование (yaml->json, логи->таблицы),
  - черновик текстов (основная LLM ревьюит).

КЛЮЧЕВОЙ ФАКТ (G3 e2e, rest#232): gemma-4-E4B-it — REASONING-модель. Без
`chat_template_kwargs={"enable_thinking": False}` она пишет "Thinking Process:..."
в content, сжигая max_tokens и ДАВАЯ ХУЖЕ ответ (проверено: с параметром 2.2s + верный
JSON, без 6s + пустой результат). Поэтому параметр стоит BY DEFAULT.

ИНТЕРФЕЙС:
  from tools.gemma_subagent import gemma_call, gemma_subagent
  r = gemma_call(task="Сведи вывод к 5 ключевым фактам", context="...до ~30k символов...")
  -> dict {ok, answer, usage, dt, est_tokens, error}
  gemma_subagent(...) — та же функция под именем инструмента (для tools_registry).
  gemma_call_file(task, file_path, max_chars=60000) — контекст из файла.
"""
import json
import time
import urllib.request

GEMMA_URL = "http://127.0.0.1:8181/v1"
GEMMA_MODEL = "gemma-4-E4B-it-Q4_0"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 1024
SYSTEM_PROMPT = (
    "Ты — саб-агент-исполнитель одноразовых задач. Отвечай ТОЧНО и компактно, "
    "без предисловий, без 'Thinking Process'. Если просят JSON — отвечай только "
    "валидным JSON (без markdown-обёртки, без пояснений). "
    "Не выдумывай: если данных не хватает — так и скажи одной строкой."
)


def _post(path: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        GEMMA_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _est_tokens(s: str) -> int:
    """Грубая оценка токенов: ~4 символа/токен — достаточно для метрики."""
    return max(1, len(s or "") // 4)


def gemma_call(
    task: str,
    context: str = "",
    json_out: bool = False,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 1,
    model: str = GEMMA_MODEL,
    thinking: bool = False,
) -> dict:
    """Один вызов саб-агента gemma.

    task     — что сделать (коротко).
    context  — сырьё (вывод/лог/JSON), до ~30k символов (32k ctx).
    json_out — если True, ответ ожидается чистым JSON (обёртка ``` снимается).
    thinking — True = разрешить reasoning-блок (реже, медленнее).
    """
    out = {"ok": False, "answer": None, "usage": None, "dt": None,
           "est_tokens": _est_tokens(context), "error": None}
    t0 = time.time()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (task +
                                             (("\n\n=== КОНТЕКСТ ===\n" + context) if context else ""))}]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            body = _post("/chat/completions", payload, timeout)
            msg = body["choices"][0]["message"]
            answer = (msg.get("content") or "").strip()
            if not answer:
                answer = (msg.get("reasoning_content") or "").strip()
            if json_out and answer.startswith("```"):
                answer = answer.strip("`")
                if answer.lower().startswith("json"):
                    answer = answer[4:]
                answer = answer.strip()
            out["ok"] = bool(answer)
            out["answer"] = answer
            out["usage"] = body.get("usage")
            if not answer:
                last_err = "empty answer (content и reasoning_content пусты)"
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            break
    out["dt"] = round(time.time() - t0, 2)
    if not out["ok"]:
        out["error"] = last_err
    return out


def gemma_call_file(task: str, file_path: str, max_chars: int = 60000, **kw) -> dict:
    """gemma_call, где контекст читается из файла (обрезка до max_chars)."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        ctx = f.read(max_chars)
    r = gemma_call(task, context=ctx, **kw)
    r["file"] = file_path
    r["context_chars"] = len(ctx)
    return r


def gemma_subagent(task: str, context: str = "", json_out: bool = False, max_tokens: int = 1024) -> str:
    """Инструмент для основной LLM: одноразовая подзадача на саб-агенте gemma (экономия контекста).

    Используй когда: нужно свести/извлечь/спарсить БОЛЬШОЙ вывод (лог, JSON, curl-ответ)
    и нести в свой контекст только КОРОТКИЙ результат. НЕ для решений/аналитики — gemma 4B слаба.

    Args:
        task: что сделать (коротко, например "извлеки HTTP-коды запросов и верни JSON {url: code}").
        context: сырьё (текст/JSON/лог), до ~30k символов.
        json_out: True если ожидаешь чистый JSON.
        max_tokens: лимит токенов ответа (по умолчанию 1024).
    Returns:
        JSON-строка {ok, answer, dt, usage, est_tokens} — answer = ответ саб-агента.
    """
    r = gemma_call(task=task, context=context, json_out=json_out, max_tokens=max_tokens)
    return json.dumps(r, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    task = sys.argv[1] if len(sys.argv) > 1 else "Ответь: 2+2?"
    ctx = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(gemma_call(task, context=ctx), ensure_ascii=False, indent=2)[:2000])
