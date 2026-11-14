"""Infinite local agent: bounded context, durable progress, explicit tool loop."""

# === LIVES+HEARTBEAT v4 (delta2) ===

import json

import logging

import os

import sys

import time

import uuid

import atexit

import signal

from datetime import datetime, timezone

from pathlib import Path



sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delta2.core.lives_report import report_start, report_alive, start_lives_watchdog, report_stopping

from langchain_core.messages import HumanMessage, ToolMessage

from langchain_core.utils.function_calling import convert_to_openai_tool

from langchain_openai import ChatOpenAI

from delta2.core.config import (

    AGENT_ROOT, LONGLIVE_AGENT_NAME, PG_CONFIG, LOG_FILE,

    TESSERACT_PATH, load_database_settings, get_runtime_settings,

)

from delta2.core.agent_checkpoint import AgentCheckpoint

from delta2.core.llama_runtime import ContextBudget, fetch_llama_config, erase_llama_slot

from delta2.core.loop_guard import LoopGuard, crash_watch, erase_with_retry

from delta2.core.longlive_memory import build_longlive_restart_prompt, write_checkpoint, load_last_checkpoint

from delta2.core.tools_registry import get_tools

from delta2.core.token_tracker import TokenTracker



def load_task_description():

    path = Path(os.environ.get('AGENT_TASK_FILE') or AGENT_ROOT / 'task.txt')

    return path.read_text(encoding='utf-8').strip() if path.exists() else ''



def save_cycle_checkpoint(task_id, cycle, recent, reason):

    # Preserve explicit remaining steps from save_progress; do not replace them with a guessed summary.

    rows, _ = load_last_checkpoint()

    payload = dict(rows[0][0]) if rows and isinstance(rows[0][0], dict) else {}

    payload.update(task_id=task_id, cycle=cycle, restart_reason=reason)

    if recent:

        payload['recent_steps'] = recent[-8:]

    write_checkpoint(payload, reasoning=reason)



def _heartbeat(conn, agent_name: str, cycle: int = 0, cycle_started: float = 0.0, last_tool_ts: float = 0.0):

    """Heartbeat v4 (f2f27df2, delta2): каждый цикл -> agent_coordination topic='heartbeat:<agent>'.



    payload {ts, cycle, cycle_started_perf, last_tool_ts}; ошибки -> logging.warning (не глотать тихо).

    """

    from psycopg2.extras import Json

    try:

        cursor = conn.cursor()

        cursor.execute(

            "INSERT INTO agent_coordination (topic, action, payload, agent) VALUES (%s, %s, %s, %s)",

            (f'heartbeat:{agent_name}', 'heartbeat',

             Json({'ts': datetime.now(timezone.utc).isoformat(), 'cycle': cycle,

                   'cycle_started_perf': round(cycle_started, 3), 'last_tool_ts': last_tool_ts}),

             agent_name))

        conn.commit()

    except Exception as exc:

        logging.warning('Heartbeat FAILED (cycle %s): %s: %s', cycle, type(exc).__name__, exc)



def context_banner(budget, settings):

    """c338c233 (human GO 02.10): context-remaining notice appended to file-write tool

    results once usage >= context_warn_percent (default 80). Cached per (usage_bucket,

    remaining) so the banner does not inflate the prompt on every write. Non-fatal:

    any failure returns '' (the agent continues without the notice)."""

    try:

        limit = getattr(budget, 'limit', 0) or 0

        if limit <= 0:

            return ''

        used = budget.last_prompt

        threshold = settings.get('context_warn_percent', 80)

        if used * 100 < limit * threshold:

            return ''

        remaining = max(0, limit - used - budget.safety)

        bucket = used // max(1, limit // 10)

        key = (bucket, remaining)

        cached = getattr(context_banner, 'cache', None)

        if cached and cached[0] == key:

            return cached[1]

        banner = (f'\n\n=== КОНТЕКСТ: использовано {used}/{limit} '

                  f'({used * 100 // max(1, limit)}%), осталось ~{remaining} токенов до рестарта '

                  f'(лимит {settings.get("context_usage_percent", 95)}% окна). '

                  f'Если шаг тяжёлый — запиши чекпоинт save_progress ДО переполнения и '

                  f'окончательно заверши текущий шаг. ===')

        context_banner.cache = (key, banner)

        return banner

    except Exception:

        return ''





def tool_result_message(call, tools, checkpoint, task_id, budget=None):

    try:

        selected = tools[call['name']]

        result = str(selected.invoke(call.get('args') or {}))

    except Exception as exc:

        result = f'TOOL ERROR: {type(exc).__name__}: {exc}'

    # Preserve full output on disk BEFORE bounding what enters the next prompt.

    output_limit = get_runtime_settings()['tool_output_max_chars']

    if len(result) > output_limit:

        path = AGENT_ROOT / 'logs' / 'tool_outputs' / f'{uuid.uuid4().hex}.txt'

        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(result, encoding='utf-8')

        result = result[:max(1,output_limit-1000)] + f'\n[Output saved in full: {path}; read it in chunks.]'

    # c338c233 (human GO 02.10): file-write tools carry the context-remaining notice.

    if budget is not None and call['name'] in ('write_local_file', 'save_progress'):

        result += context_banner(budget, get_runtime_settings())

    checkpoint.save_message(task_id, 'tool', result, tool_call_id=call['id'])

    return ToolMessage(content=result, tool_call_id=call['id'], name=call['name'])



def run():

    # OCR remains installed/available as requested; only initialize it at runtime.

    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, logging.StreamHandler()])

    checkpoint = None

    try:

        while checkpoint is None:

            try:

                checkpoint = AgentCheckpoint(PG_CONFIG)

                load_database_settings()

                task_id = checkpoint.find_running_task(LONGLIVE_AGENT_NAME)

                if not task_id:

                    task_id = checkpoint.create_task(load_task_description(), LONGLIVE_AGENT_NAME, 'pending discovery')

                stored_task_description = checkpoint.load_original_task(task_id)

                # FIX 3256f717 (24.09): task text for the prompt comes from task.txt ONLY.

                # tasks.original_prompt is audit-only by default (frozen 22.09 boilerplate

                # was re-injected as 'current task' on every restart). Opt back in with

                # agent_settings key task_from_db_fallback=true.

                if not get_runtime_settings().get("task_from_db_fallback", False):

                    stored_task_description = ""

            except Exception:

                logging.exception('Database initialization failed; retrying')

                if checkpoint is not None:

                    checkpoint.close()

                    checkpoint = None

                time.sleep(get_runtime_settings()['restart_delay_seconds'])

        os.environ['AGENT_TASK_ID'] = task_id

        # lives (заказ человека 2026-09-23): при старте — upsert + restart_count + время старта

        report_start(checkpoint, LONGLIVE_AGENT_NAME, task_id)

        # 2a23c2d2 S3 (mirror of bravo S2; design lock plan/2a23c2d2_plan.md):

        # watchdog-поток держит lives.last_heartbeat_at свежим во время длинных LLM-циклов,

        # backoff (до 900с) и deadlock в tool-call (ложный «мёртв» якоря — кейс bbd8796d).

        # atexit/signal: graceful stop -> note='STOPPING (reason)'. kill -9 не перехватить

        # (ts stale -> якорь корректно «мёртв» через 1ч — это детекция реального краха).

        try:

            _wb_settings = get_runtime_settings()

            start_lives_watchdog(None, LONGLIVE_AGENT_NAME, int(_wb_settings.get('lives_heartbeat_seconds', 120)))

        except Exception as _wb_exc:

            logging.warning('lives watchdog start FAILED (не роняем агента): %s', _wb_exc)

        try:

            atexit.register(report_stopping, None, LONGLIVE_AGENT_NAME, 'atexit')



            def _lives_stop_handler(signum, _frame):

                try:

                    report_stopping(None, LONGLIVE_AGENT_NAME, f'signal:{signum}')

                    from delta2.core.config import PG_CONFIG as _PG

                    import psycopg2 as _pg2

                    _conn = _pg2.connect(**_PG)

                    _cur = _conn.cursor()

                    _cur.execute("INSERT INTO agent_coordination (topic, action, payload, agent) VALUES (%s, %s, %s::jsonb, %s)",

                                 (f'stop:{LONGLIVE_AGENT_NAME}', 'stopping', json.dumps({'reason': f'signal:{signum}'}), LONGLIVE_AGENT_NAME))

                    _conn.commit(); _conn.close()

                except Exception as _stop_exc:

                    logging.warning('lives stop-report FAILED: %s', _stop_exc)

                logging.info('lives: STOPPING reported (signal %s); exiting gracefully', signum)

                sys.exit(0)



            for _signame in ('SIGTERM', 'SIGABRT', 'SIGBREAK'):

                _signum = getattr(signal, _signame, None)

                if _signum is None:

                    continue

                try:

                    signal.signal(_signum, _lives_stop_handler)

                except Exception as _sigreg_exc:

                    logging.warning('lives signal %s register FAILED: %s', _signame, _sigreg_exc)

        except Exception as _atexit_exc:

            logging.warning('lives atexit/signal register FAILED: %s', _atexit_exc)

        # diegle_ensure (294ad76a s2, delta2): демон diegle-summary поднимается из bootstrap агента

        # (НЕ из Task Scheduler). Дедуп: если демон уже запущен — дубль не создаётся.

        # NON-FATAL: любая ошибка -> log, агент продолжает.

        try:

            import os as _osd_g

            if _osd_g.path.exists(r"<AGENTS_DIR>\workdir\lfm2_digest\DIEGLE_AUTO_OFF.marker"):

                logging.info('diegle_ensure SKIPPED: manual mode via lfm2_digest_ctl.bat (GO 33a2199a)')

            else:

                from delta2.core.diegle_ensure import ensure_diegle

                _dg = ensure_diegle(agent=LONGLIVE_AGENT_NAME)

                logging.info('diegle_ensure: %s', _dg)

        except Exception as _dg_exc:

            logging.warning('diegle_ensure FAILED (не роняем агента): %s', _dg_exc)

        # llm_lives_ensure (e61f4b08 s3, delta2): LLM-серверы-цели (agent_settings.llm_lives_targets)

        # сканируются при старте/рестарте агента: TCP 8080-9090 + GET /v1/models -> таблица llm_lives.

        # Детач (bootstrap НЕ ждёт), дедуп по процессу, lockfile TTL 10s, NON-FATAL.

        try:

            from delta2.core.llm_lives_ensure import ensure_llm_lives

            _lll = ensure_llm_lives(agent=LONGLIVE_AGENT_NAME)

            logging.info('llm_lives_ensure: %s', _lll)

        except Exception as _lll_exc:

            logging.warning('llm_lives_ensure FAILED (не роняем агента): %s', _lll_exc)

        # автоархивация при старте (ГО человека 24.09, 1ed0ea3a; v3.3 в коде; delta2 -> потом bravo)

        try:

            from delta2.core.startup_archive import run_startup_archive

            _arch = run_startup_archive(checkpoint, LONGLIVE_AGENT_NAME)

            logging.info('startup archive: %s', _arch)

        except Exception as _arch_exc:  # не роняем агента

            logging.warning('startup archive FAILED: %s', _arch_exc)

        crash_watch(LONGLIVE_AGENT_NAME, task_id)

        tool_list = get_tools()

        tools = {item.name: item for item in tool_list}

        schemas = [convert_to_openai_tool(item) for item in tool_list]

        guard = LoopGuard(LONGLIVE_AGENT_NAME, task_id)

        cycle = 0

        while True:

            recent, reason = [], 'generation complete'

            cycle_started = time.perf_counter()

            last_tool_ts = time.time()  # heartbeat v4: время последнего tool-call

            cycle_had_tools = False  # loop-guard (7881bfef)

            # 85c9e66a: controlled-restart state (human GO d7581a4c); never fatal

            try:

                from delta2.tools import request_restart as _cr_rr

                _cr_rr.reset()

            except Exception:

                pass

            bootstrap_compacted = False

            cycle_model, cycle_slot = 'undetected', 'undetected'

            _cr_noted = False  # 85c9e66a: system-note once per cycle

            # lives (заказ человека 2026-09-23): каждый цикл — агент жив

            try:

                report_alive(checkpoint, LONGLIVE_AGENT_NAME, cycle)

            except Exception as _lves_exc:

                logging.warning('lives alive failed: %s', _lves_exc)

            try:

                settings = load_database_settings()

                cycle_settings = get_runtime_settings()

                logging.getLogger().setLevel(cycle_settings['log_level'])

                cycle_slot = cycle_settings['agent_slot']

                config = fetch_llama_config(cycle_settings)

                cycle_model = config['model_name']

                no_tool_responses = 0

                logging.info('Cycle %s: context=%s (%s), completion=%s, slot=%s', cycle, config['n_ctx'], config['context_source'], config['max_tokens'], cycle_settings['agent_slot'])

                if not erase_with_retry(settings=cycle_settings):

                    raise RuntimeError('Slot was not erased; retry before sending a new context')

                llm = ChatOpenAI(model=config['model_name'], base_url=cycle_settings['llama_server_url'],

                    api_key=cycle_settings['llama_api_key'], max_tokens=config['max_tokens'], timeout=cycle_settings['model_timeout_seconds'],

                    max_retries=0, extra_body={'id_slot': cycle_settings['agent_slot']}).bind_tools(tool_list)

                budget = ContextBudget(config['n_ctx'], config['max_tokens'], schemas, settings=cycle_settings)

                tracker = TokenTracker(config['n_ctx'])

                # c338c233: immediate context notice if a cycle starts already above threshold

                # (bootstrap+memory prompt alone can exceed 80% on small windows).

                _bn = context_banner(budget, cycle_settings)

                if _bn:

                    logging.info('Context at cycle start: %s', _bn.strip())

                prompt = build_longlive_restart_prompt(load_task_description() or stored_task_description, cycle)  # FIX 3256f717: stored is '' unless task_from_db_fallback

                messages = [HumanMessage(content=prompt)]

                checkpoint.update_task_status(task_id, 'running', config['model_name'])

                checkpoint.save_message(task_id, 'user', prompt)

                while True:

                    load_database_settings()

                    settings = get_runtime_settings()

                    budget.update_policy(settings)

                    if not budget.can_call(messages):

                        reason = f'context reserve reached: prompt={budget.last_prompt}, window={budget.context}, configured_limit={budget.limit}, remaining_completion={budget.last_completion}, source={budget.source}'

                        if len(messages) == 1:

                            if not bootstrap_compacted:

                                compact_prompt = build_longlive_restart_prompt(

                                    load_task_description() or stored_task_description, cycle, compact=True)  # FIX 3256f717: same rule as above

                                if len(compact_prompt) < len(prompt):

                                    logging.warning('Bootstrap prompt oversized; rebuilding compact (%d -> %d chars)',

                                                   len(prompt), len(compact_prompt))

                                    prompt = compact_prompt

                                    messages = [HumanMessage(content=prompt)]

                                    bootstrap_compacted = True

                                    continue

                            raise ValueError('Bootstrap/task prompt exceeds context budget; shorten task.txt or bootstrap_prompt.yaml')

                        break

                    logging.info('Context budget: prompt=%s window=%s policy_limit=%s (%s%%) max_tokens=%s source=%s', budget.last_prompt, budget.context, budget.limit, settings['context_usage_percent'], budget.last_completion, budget.source)

                    # cfa94560-F2-retry + F3-correction (803130fa, delta2 rest#14 04.10):

                    # 500 'Failed to parse tool call arguments' = malformed JSON in a

                    # tool-call arg. F2 bare re-send FAILED: generation is deterministic,

                    # the model reproduced the same broken 5KB call 3x (ctx 46.8k/164k,

                    # NOT overflow) -> cycle restart. F3: on each failed attempt append a

                    # short correction HumanMessage (split big writes into small chunks)

                    # so the model fixes its own call; restore messages on final failure.

                    reply = None

                    for _cfa_attempt in range(1, 4):

                        _cfa_len0 = len(messages)

                        try:

                            reply = llm.invoke(messages, max_tokens=budget.last_completion)

                            break

                        except Exception as _cfa_exc:

                            _cfa_msg = str(_cfa_exc)

                            if 'Failed to parse tool call' not in _cfa_msg:

                                raise

                            if _cfa_attempt >= 3:

                                del messages[_cfa_len0:]  # drop correction, restore state

                                raise

                            # reply is None here (server 500 before content); append ONLY

                            # the correction so the model re-issues a smaller tool call.

                            try:

                                messages.append(HumanMessage(

                                    content=(f'SERVER REJECTED YOUR LAST TOOL CALL (attempt {_cfa_attempt}/3): '

                                             'invalid JSON argument, almost certainly because the call was '

                                             'TRUNCATED by the token limit (max_tokens). HARD RULE: never emit '

                                             'a tool call with any argument longer than ~1000 characters. If '

                                             'you were writing a big file: write only the first ~700 characters '

                                             'now, then append the rest via run_python_script using '

                                             "open(path, 'a', encoding='utf-8'). Do NOT repeat the failed call. "

                                             'Make the next SMALL tool call now. Reply as briefly as possible: '

                                             'do NOT re-state prior context or re-read files you already have. ')))

                            except Exception:

                                pass  # never fatal: correction is best-effort

                            logging.warning('LLM 500 bad tool-call JSON (attempt %s/3), '

                                            'retrying with correction in 5s: %s',

                                            _cfa_attempt, _cfa_msg[:200])

                            time.sleep(5)

                            if not erase_with_retry(settings=cycle_settings):

                                del messages[_cfa_len0:]

                                raise RuntimeError('Slot not clean after JSON-500 retry; aborting cycle')

                    assert reply is not None
                    # 4d9385e6 S5 (alfa rest#39): d1/d3 = REASONING-модели (Bonsai2-27B / Spark-X2.6).
                    # llama.cpp отдаёт текстовый ответ в reasoning_content, content="". Без fallback
                    # агент видит пустой ответ (зацикливание). d2 (gemma) = стандартная (content заполнен).
                    # НЕ-ДЕСТРУКТИВ: заполняем content только когда он пуст И нет tool_calls.
                    if not reply.content and not reply.tool_calls:
                        _rc = (getattr(reply, 'additional_kwargs', None) or {}).get('reasoning_content') or ''
                        if _rc:
                            reply.content = _rc
                            logging.info('reasoning_content fallback applied (%d chars)', len(_rc))

                    metrics = tracker.update_from_message(reply)

                    budget.observe(metrics['prompt_tokens'])

                    checkpoint.save_message(task_id, 'assistant', str(reply.content), reply.tool_calls, token_metrics=metrics)

                    messages.append(reply)

                    recent.append({'role': 'assistant', 'content': str(reply.content)[-1500:],

                                   'tools': [c['name'] for c in reply.tool_calls]})

                    logging.info('Prompt=%s completion=%s; %s', metrics['prompt_tokens'], metrics['completion_tokens'], str(reply.content)[:600])

                    for call in reply.tool_calls:

                        result = tool_result_message(call, tools, checkpoint, task_id, budget)

                        messages.append(result)

                        recent.append({'role': 'tool', 'name': call['name'], 'content': result.content[-1000:]})

                        last_tool_ts = time.time()

                        # 85c9e66a: controlled-restart bookkeeping (never fatal)

                        try:

                            from delta2.tools import request_restart as _cr_rr

                            if call['name'] == 'save_progress':

                                _cr_rr.mark_checkpoint_saved()

                            _cr_rr._RESTART_STATE['cycle_tool_calls'] += 1

                            _cr_rr._RESTART_STATE['usage_percent'] = round(budget.last_prompt * 100 / max(1, budget.limit), 1)

                            if _cr_rr._RESTART_STATE['requested']:

                                reason = ('controlled_restart: ' + _cr_rr._RESTART_STATE['reason']

                                          + f' | usage={_cr_rr._RESTART_STATE["usage_percent"]}%, tool_calls={_cr_rr._RESTART_STATE["cycle_tool_calls"]}')

                                break

                            _cr_thr = settings.get('controlled_restart_threshold', 80)

                            if (not _cr_noted) and budget.last_prompt * 100 >= budget.limit * _cr_thr:

                                _cr_noted = True

                                messages.append(HumanMessage(content=(

                                    'NOTE (controlled-restart, 85c9e66a): контекст уже выше порога планового рестарта. '

                                    'Если текущий шаг завершён или ты переходишь к менее важной работе — '

                                    'сделай save_progress, затем request_restart(reason). '

                                    'Если шаг критичный и короткий — можно доделать и остановиться.')))

                        except Exception:

                            pass  # never fatal: 95% hard limit remains the safety net

                    if reply.tool_calls:

                        no_tool_responses = 0

                        cycle_had_tools = True  # loop-guard: прогресс в цикле

                    else:

                        # 27109a18 FIX (delta2, 13.10): controlled restart APPROVED but the

                        # model finished the turn with TEXT ONLY (no tool calls). Previously

                        # the continuation branch kept the cycle running and the 1-per-cycle

                        # limit then blocked re-request -> "restart never happens".

                        try:

                            from delta2.tools import request_restart as _cr_rr27109

                            if _cr_rr27109._RESTART_STATE.get('requested'):

                                reason = ('controlled_restart: ' + _cr_rr27109._RESTART_STATE['reason']

                                          + f' | usage={_cr_rr27109._RESTART_STATE["usage_percent"]}%, '

                                            f'tool_calls={_cr_rr27109._RESTART_STATE["cycle_tool_calls"]}')

                                logging.info('Controlled restart APPROVED on text-only reply -> ending cycle now: %s', reason)

                                break

                        except Exception:

                            pass

                        no_tool_responses += 1

                        logging.info('Replies without tools: %s/%s', no_tool_responses, settings['no_tool_response_limit'])

                        if no_tool_responses >= settings['no_tool_response_limit']:

                            reason = f'no tool calls in {no_tool_responses} consecutive replies'

                            break

                        save_cycle_checkpoint(task_id, cycle, recent, 'continuing in the same context')

                        continuation = ('Продолжай работу по текущей задаче и оставшимся шагам. '

                            'Контекст сохранён, это не перезапуск. Если этап завершён, сохрани прогресс '

                            'и проверь новые задачи/ответы человека. Не повторяй выполненные действия.')

                        messages.append(HumanMessage(content=continuation))

                        checkpoint.save_message(task_id, 'user', continuation)

                        time.sleep(get_runtime_settings()['restart_delay_seconds'])

            except Exception as exc:

                reason = f'{type(exc).__name__}: {str(exc)[:1000]}'

                logging.exception('Cycle failed; agent will continue')

                try:

                    checkpoint.save_message(task_id, 'system', reason)

                except Exception:

                    logging.exception('Failed to persist cycle error')

            finally:

                logging.info('Cycle %s finished: dur_ms=%.1f model=%s slot=%s; restart reason: %s',

                             cycle, (time.perf_counter()-cycle_started)*1000, cycle_model, cycle_slot, reason)

                try:

                    save_cycle_checkpoint(task_id, cycle, recent, reason)

                except Exception:

                    logging.exception('Checkpoint DB write failed; saving local recovery file')

                    recovery = AGENT_ROOT / 'logs' / f'cycle_recovery_{time.time_ns()}.json'

                    recovery.write_text(json.dumps({'task_id': task_id, 'agent': LONGLIVE_AGENT_NAME,

                        'recent_steps': recent[-8:], 'restart_reason': reason}, ensure_ascii=False), encoding='utf-8')

                cycle += 1

            guard.state['last_model'] = cycle_model

            guard.state['last_slot'] = cycle_slot

            try:

                guard_sleep = guard.on_cycle_finished(reason, cycle_had_tools)

            except Exception:

                logging.exception('loop_guard failed; default delay')

                guard_sleep = get_runtime_settings()['restart_delay_seconds']

            # heartbeat v4: каждый цикл (дешёвая запись), Z3 по времени

            try:

                with checkpoint._get_conn() as conn:

                    _heartbeat(conn, LONGLIVE_AGENT_NAME, cycle, cycle_started, last_tool_ts)

                logging.info('Heartbeat #%s written (topic=heartbeat:%s)', cycle, LONGLIVE_AGENT_NAME)

            except Exception:

                logging.warning('Heartbeat write failed (cycle %s)', cycle)

            time.sleep(guard_sleep)

    except KeyboardInterrupt:

        logging.info('Stopped by human; task remains running for resume')

    finally:

        try:

            report_stopping(None, LONGLIVE_AGENT_NAME, 'process-exit')

        except Exception as _pexit_exc:

            logging.warning('lives process-exit report FAILED: %s', _pexit_exc)

        if checkpoint is not None:

            checkpoint.close()

        logging.getLogger().removeHandler(file_handler)

        file_handler.close()



if __name__ == '__main__':

    run()

