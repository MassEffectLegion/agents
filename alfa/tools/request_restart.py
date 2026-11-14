"""85c9e66a: controlled (planned) cycle restart for ALFA harness.

Human GO: d7581a4c (07.10). Design: plan/85c9e66a_analysis_bravo.md,
patch spec: plan/85c9e66a_patch_alfa_harness.md.
Non-fatal by construction: main.py wraps all integration in try/except.

27109a18 (13.10, human task): rate limit = controlled_restart_max_per_window (default 2)
APPROVED restarts per controlled_restart_window_seconds (default 1800s / 30 min), tracked in
logs/controlled_restart_state.json (survives process restarts). The 1-per-cycle flag remains
as a sanity guard. Root cause of "restart never happens": main.py checked the requested flag
only inside the tool_calls loop, so a text-only reply after APPROVED hit the continuation
branch and the cycle kept running.
"""
import logging

from langchain_core.tools import tool

_RESTART_STATE = {'requested': False, 'reason': '', 'validated_at': 0.0,
                  'cycle_tool_calls': 0, 'usage_percent': 0, 'checkpoint_age': 0.0}

def _state():
    return dict(_RESTART_STATE)

def reset():
    """Call at the start of every cycle: fresh flags, old checkpoint (age = cycle start)."""
    import time
    _RESTART_STATE['requested'] = False
    _RESTART_STATE['reason'] = ''
    _RESTART_STATE['validated_at'] = 0.0
    _RESTART_STATE['cycle_tool_calls'] = 0
    _RESTART_STATE['usage_percent'] = 0
    _RESTART_STATE['checkpoint_age'] = time.time()

def mark_checkpoint_saved():
    """Called by main.py after a successful save_progress tool call."""
    import time
    _RESTART_STATE['checkpoint_age'] = time.time()


# 27109a18: rate-limit state (survives process restarts)
_RATE_STATE_FILE = None


def _rate_state_path():
    global _RATE_STATE_FILE
    if _RATE_STATE_FILE is None:
        from alfa.core.config import AGENT_ROOT
        _RATE_STATE_FILE = AGENT_ROOT / 'logs' / 'controlled_restart_state.json'
    return _RATE_STATE_FILE


def _load_rate_log():
    import json
    try:
        data = json.loads(_rate_state_path().read_text(encoding='utf-8'))
        if isinstance(data, list):
            return [float(x) for x in data if isinstance(x, (int, float))]
    except Exception:
        pass
    return []


def _record_approved():
    import json, time
    log = _load_rate_log()
    log.append(time.time())
    try:
        _rate_state_path().parent.mkdir(parents=True, exist_ok=True)
        _rate_state_path().write_text(json.dumps(log[-20:]), encoding='utf-8')
    except Exception as exc:
        logging.warning('request_restart: rate state save failed: %s', exc)


def _recent_approved(window_seconds):
    import time
    now = time.time()
    return [t for t in _load_rate_log() if now - t <= window_seconds]

@tool
def request_restart(reason: str):
    """Запросить ПЛАНОВЫЙ рестарт цикла (controlled restart).

    Вызывать когда: (а) контекст близок к концу (порог controlled_restart_threshold,
    дефолт 80%), (б) в цикле уже >= min_tool_calls_for_controlled_restart (3) tool-вызовов,
    (в) чекпоинт свежий (save_progress за последние ~5 мин) — сначала вызови save_progress.

    После успешного запроса цикл завершится ПОСЛЕ ТЕКУЩЕГО ШАГА (чисто, не в полуслове).
    reason = короткое объяснение (попадёт в промпт следующего цикла).
    Лимит: не более 1 controlled-рестарта за цикл.
    """
    import time
    from alfa.core.config import get_runtime_settings
    s = get_runtime_settings()
    thr = s.get('controlled_restart_threshold', 80)
    min_calls = s.get('min_tool_calls_for_controlled_restart', 3)
    if not reason or len(reason.strip()) < 8:
        return 'REJECTED: reason слишком короткий (>= 8 символов). Укажи, зачем рестарт.'
    if _RESTART_STATE['requested']:
        return 'REJECTED: controlled-restart уже запрошен в этом цикле (лимит 1/цикл).'
    max_per_window = int(s.get('controlled_restart_max_per_window', 2))
    window = int(s.get('controlled_restart_window_seconds', 1800))
    if max_per_window > 0:
        recent = _recent_approved(window)
        if len(recent) >= max_per_window:
            oldest = max(recent)
            wait_min = max(1, int((window - (time.time() - oldest)) // 60) + 1)
            return (f'REJECTED: лимит {max_per_window} controlled-рестарта за {window // 60} мин исчерпан '
                    f'({len(recent)} в окне). Повтори не ранее чем через ~{wait_min} мин '
                    '(или жди обычный рестарт по 95% контекста).')
    cnt = _RESTART_STATE.get('cycle_tool_calls', 0)
    usage = _RESTART_STATE.get('usage_percent', 0)
    if usage < thr:
        return (f'REJECTED: usage {usage}% < threshold {thr}%. '
                f'Controlled-restart только когда контекст реально близок к концу.')
    if cnt < min_calls:
        return (f'REJECTED: только {cnt} tool-вызовов за цикл (< {min_calls}). '
                f'Рестартовать почти пустой цикл — бессмысленно.')
    age = time.time() - _RESTART_STATE.get('checkpoint_age', 0.0)
    if age > 300:
        return ('REJECTED: чекпоинт не свежий (save_progress > 5 мин назад). '
                'Сначала вызови save_progress с done/results/remaining, потом request_restart.')
    _RESTART_STATE['requested'] = True
    _RESTART_STATE['reason'] = reason.strip()[:300]
    _RESTART_STATE['validated_at'] = time.time()
    _record_approved()  # 27109a18: count toward the 2/30min window
    return ('APPROVED: цикл завершится после текущего шага (controlled restart). '
            f'reason="{_RESTART_STATE["reason"]}". Сделай завершающий шаг и остановись.')
