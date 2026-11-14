"""K1 (aa9aa7b6, GO человека 08.11 a64205c4): Tool-call JSON pre-flight validator.

Проблема: некоторые LLM-серверы (llama.cpp) возвращают аргументы tool-call как
СТРОКУ (даже повреждённую/обрезанную), либо нарушают types/required-схему.
Старое поведение: args='' -> {} -> TOOL ERROR (TypeError: missing required arg)
или сервер-500 'Failed to parse tool call arguments' (детерминированный повтор
-> F3-ретраи 3x -> аварийный рестарт цикла). Это была главная повторяющаяся
причина смерти циклов (3+ рестарта).

Решение: ДО execute — валидация arguments:
  1) парсинг (dict готов / JSON-строка / попытка-репары);
  2) required-параметры;
  3) базовые types по annotations функции инструмента;
  4) при ошибке — ToolMessage с ТРЕБУЕМЫМ действием (model сам исправляет),
     один авто-ретрай; второй раз — та же ошибка (без рестарта цикла).

Модуль самодостаточен (только stdlib); main.py использует только validate_call().
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_TYPE_MAP = {
    'str': str,
    'int': int,
    'float': float,
    'bool': bool,
    'list': list,
    'dict': dict,
    'tuple': tuple,
    'bytes': bytes,
}


def _coerce_args(args: Any) -> tuple[Any, str | None]:
    """Возвращает (args_dict, error). dict -> как есть; str -> JSON-парсинг (+репары)."""
    if args is None:
        return {}, None
    if isinstance(args, dict):
        return args, None
    if isinstance(args, (list, tuple)):
        return {'_args_list': list(args)}, None
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}, None
        try:
            v = json.loads(s)
            if isinstance(v, dict):
                return v, None
            return {'_value': v}, None
        except Exception as e1:
            # репары: обрезанный хвост (токен-лимит) -> закрыть скобки/кавычки
            for cand in _repair_candidates(s):
                try:
                    v = json.loads(cand)
                    if isinstance(v, dict):
                        return v, None  # note проставляется в validate_call
                except Exception:
                    continue
            return None, f'invalid JSON: {e1}'
    return None, f'неожиданный тип args: {type(args).__name__}'


def _repair_candidates(s: str) -> list[str]:
    """Кандидаты репары обрезанного JSON (по убыванию сохранения данных).

    Приоритет: (1) закрыть незакрытую строку — частичное значение СОХРАНЯЕТСЯ
    (write_local_file получает первый чанк; модель дозапишет остаток append'ом);
    (2) оборвать на последней полной паре key-value и закрыть объект.
    """
    t = s.rstrip()
    cands = []
    # 1) незакрытая строка: закрыть кавычку + закрыть скобки
    for closer in ('"', '""'):
        for obj_close in ('}', '}]', '}}', ']]'):
            cands.append(t + closer + obj_close)
    # 2) последняя запятая: оборвать неполную пару и закрыть
    for cut in range(len(t) - 1, max(0, len(t) - 400), -1):
        if t[cut] == ',':
            head = t[:cut].rstrip()
            for obj_close in ('}', '}]', '}}', ']]'):
                cands.append(head + obj_close)
            break
    # 3) просто закрыть (в конце могла быть обрезана строка без запятой)
    cands.append(t.rstrip(',').rstrip() + '"}')
    return cands


def _check_required(args: dict, sig: inspect.Signature) -> str | None:
    for name, p in sig.parameters.items():
        if name in ('args', 'kwargs'):
            continue
        if name in args:
            continue
        if p.default is inspect.Parameter.empty:
            return f'missing required parameter: {name}'
    return None


def _check_types(args: dict, sig: inspect.Signature) -> str | None:
    for name, p in sig.parameters.items():
        if name not in args or name in ('args', 'kwargs'):
            continue
        ann = p.annotation
        tname = getattr(ann, '__name__', None) or (ann if isinstance(ann, str) else None)
        tname = getattr(ann, '__name__', None) if not isinstance(ann, str) else ann
        t = _TYPE_MAP.get(tname or '')
        v = args[name]
        if t is None or v is None:
            continue
        if t is bool:
            if not isinstance(v, bool):
                return f'wrong type for {name}: expected bool, got {type(v).__name__}'
        elif t is int:
            if isinstance(v, bool) or not isinstance(v, int):
                # численная строка допустима (LLM часто пишет int как "100")
                try:
                    args[name] = int(v)
                except Exception:
                    return f'wrong type for {name}: expected int, got {type(v).__name__} ({v!r})'
        elif t is float:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                try:
                    args[name] = float(v)
                except Exception:
                    return f'wrong type for {name}: expected float, got {type(v).__name__} ({v!r})'
        else:
            if not isinstance(v, t):
                # строка для list/dict: попробуем разобрать как JSON
                if isinstance(v, str) and t in (list, dict):
                    try:
                        args[name] = json.loads(v)
                        continue
                    except Exception:
                        pass
                if isinstance(v, str) and t is str:
                    continue
                return f'wrong type for {name}: expected {t.__name__}, got {type(v).__name__} ({str(v)[:80]!r})'
    return None


def _resolve_fn(fn: Any) -> Any:
    """Пройти цепочку обёрток (_LoggedTool.inner -> StructuredTool.func -> callable)."""
    for _ in range(4):
        nxt = getattr(fn, 'func', None) or getattr(fn, '__wrapped__', None) or getattr(fn, 'inner', None)
        if nxt is None:
            break
        fn = nxt
    return fn if callable(fn) else None


def _base_type(ann: Any):
    import typing
    for _ in range(3):  # Optional[X] = Union[X, None]
        if typing.get_origin(ann) in (typing.Union,):
            inner = [a for a in typing.get_args(ann) if a is not type(None)]
            ann = inner[0] if len(inner) == 1 else None
            continue
        break
    # generic-алиасы: list[str] -> list, dict[str, X] -> dict
    origin = typing.get_origin(ann)
    if origin is not None:
        return _TYPE_MAP.get(origin.__name__)
    return _TYPE_MAP.get(ann.__name__) if isinstance(ann, type) else None


def validate_call(name: str, fn: Any, args: Any) -> tuple[Any, str | None, str | None]:
    """Pre-flight валидация tool-call ДО execute.

    fn: callable инструмента ИЛИ BaseTool-обёртка (берём __wrapped__/func/inner
    и/или args_schema.model_fields как fallback).
    Возврат: (parsed_args, error, note)
      error=None  -> parsed_args можно передавать в invoke
      error=str   -> невалидно; текст ошибки готов для ToolMessage-ретрая
      note        -> info (напр. 'repaired-tail') для логирования
    """
    raw = _resolve_fn(fn)
    sig = None
    if raw is not None:
        try:
            sig = inspect.signature(raw)
        except Exception:
            sig = None
    model_fields = None
    if sig is None:
        schema = getattr(fn, 'args_schema', None) or getattr(raw, 'args_schema', None)
        model_fields = getattr(schema, 'model_fields', None)
    parsed, err = _coerce_args(args)
    if err is not None:
        return None, err, None
    note = None
    if parsed is not None and isinstance(args, str):
        try:
            json.loads(args.strip())
        except Exception:
            note = 'repaired-tail'
    rerr = None
    if sig is not None:
        rerr = _check_required(parsed, sig)
        if rerr is None:
            rerr = _check_types(parsed, sig)
    elif model_fields:
        for mname, field in model_fields.items():
            if mname not in parsed and field.is_required():
                rerr = f'missing required parameter: {mname}'
                break
        if rerr is None:
            for mname, field in model_fields.items():
                if mname not in parsed:
                    continue
                t = _base_type(field.annotation)
                v = parsed[mname]
                if t is None or v is None:
                    continue
                if t is bool:
                    if not isinstance(v, bool):
                        rerr = f'wrong type for {mname}: expected bool, got {type(v).__name__}'
                        break
                elif t is int:
                    if isinstance(v, bool) or not isinstance(v, int):
                        try:
                            parsed[mname] = int(v)
                        except Exception:
                            rerr = f'wrong type for {mname}: expected int, got {type(v).__name__} ({v!r})'
                            break
                elif t is float:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        try:
                            parsed[mname] = float(v)
                        except Exception:
                            rerr = f'wrong type for {mname}: expected float, got {type(v).__name__} ({v!r})'
                            break
                elif t in (list, dict) and isinstance(v, str):
                    try:
                        parsed[mname] = json.loads(v)
                    except Exception:
                        rerr = f'wrong type for {mname}: expected {t.__name__}, got str'
                        break
                elif t is str and not isinstance(v, str):
                    rerr = f'wrong type for {mname}: expected str, got {type(v).__name__}'
                    break
    if rerr is not None:
        return parsed, rerr, note
    return parsed, None, note


def error_tool_message_content(name: str, err: str, attempt: int) -> str:
    """Текст ToolMessage для авто-реты: конкретно + требуемое действие."""
    if attempt >= 2:
        return (f'TOOL ERROR (final, {name}): {err} — retry exhausted for this call. '
                'Re-issue the call with a CORRECTED, SMALLER payload: split the content into '
                'several small calls (~700 chars each). Do NOT repeat the same broken call.')
    return (f'TOOL ERROR (attempt 1, auto-retry allowed, {name}): {err}. '
            'Your tool-call arguments were REJECTED before execution. Fix the JSON '
            '(valid object, required params, correct types) and/or shorten long arguments, '
            'then re-issue the SAME tool call with corrected arguments. '
            'If an argument is large (file text, SQL, report), split it: first chunk now, '
            'rest via append (run_python_script: open(path, "a", encoding="utf-8")).')
