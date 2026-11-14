"""Infinite local agent: bounded context, durable progress, explicit tool loop."""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from bravo.core.config import (
    AGENT_ROOT, LONGLIVE_AGENT_NAME, PG_CONFIG, LOG_FILE,
    TESSERACT_PATH, load_database_settings, get_runtime_settings,
)
from bravo.core.agent_checkpoint import AgentCheckpoint
from bravo.core.llama_runtime import ContextBudget, fetch_llama_config, erase_llama_slot
from bravo.core.longlive_memory import build_longlive_restart_prompt, write_checkpoint, load_last_checkpoint
from bravo.core.lives_report import report_start, report_alive, start_lives_watchdog, report_stopping
from bravo.core.loop_guard import LoopGuard, crash_watch, erase_with_retry
from bravo.core.tools_registry import get_tools
from bravo.core.token_tracker import TokenTracker

def load_task_description():
    path = Path(os.environ.get('AGENT_TASK_FILE') or AGENT_ROOT / 'task.txt')
    return path.read_text(encoding='utf-8').strip() if path.exists() else ''

def save_cycle_checkpoint(task_id, cycle, recent, reason):
    rows, _ = load_last_checkpoint()
    payload = dict(rows[0][0]) if rows and isinstance(rows[0][0], dict) else {}
    payload.update(task_id=task_id, cycle=cycle, restart_reason=reason)
    if recent:
        payload['recent_steps'] = recent[-8:]
    write_checkpoint(payload, reasoning=reason)

def _heartbeat(conn, agent_name: str, cycle: int = 0, cycle_started: float = 0.0, last_tool_ts: float = 0.0):
    """Write a heartbeat entry to agent_coordination topic='heartbeat:<agent>'."""
    import psycopg2
    from psycopg2.extras import Json
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_coordination (topic, action, payload, agent)
            VALUES (%s, %s, %s, %s)
        """, (f'heartbeat:{agent_name}', 'heartbeat', 
              Json({'ts': datetime.now(timezone.utc).isoformat(), 'cycle': cycle, 'cycle_started_perf': cycle_started, 'last_tool_ts': last_tool_ts}), 
              agent_name))
        conn.commit()
    except Exception as exc:
        logging.warning('Heartbeat FAILED (DB problem signal, must be visible): %s: %s', type(exc).__name__, exc)

def merge_trailing_assistant(messages):
    """FIX c46 (13.10, rest#44) + FIX c49 (13.10, rest#47): merge trailing assistant
    messages before llm.invoke. c49 correction: langchain_core AIMessage.type is "ai"
    (not "assistant"), so the c46 version was a NO-OP; now accepts "ai" OR "assistant".

    no_tool_response_limit=3 (human-approved, 1a0200a9/db6768c8) allows up to 3
    consecutive no-tool replies. Two consecutive no-tool replies leave the tail
    [assistant, assistant]; llama-server (OpenAI-compatible) rejects that with
    400 'Cannot have 2 or more assistant messages at the end of the list' ->
    cycle dies -> fast restart (caused restart #44 after ~6s). The merge keeps
    the human limit=3 decision intact while making every API call valid: it
    concatenates the trailing no-tool assistant contents (newest stays last)
    into ONE assistant message. Returns count of messages removed (0 = no-op).
    Non-fatal: any error -> list unchanged.
    """
    try:
        i = len(messages) - 1
        while i > 0 and getattr(messages[i], 'type', 'human') in ('assistant', 'ai') \
                and not getattr(messages[i], 'tool_calls', None):
            i -= 1
        # c49: if the FIRST message is also a plain assistant (whole list), include it
        if i == 0 and getattr(messages[0], 'type', 'human') in ('assistant', 'ai') \
                and not getattr(messages[0], 'tool_calls', None):
            i = -1
        if i == len(messages) - 1:
            return 0
        tail = messages[i + 1:]
        if len(tail) < 2:
            return 0
        contents = [str(getattr(m, 'content', '') or '') for m in tail]
        merged = '\n---\n'.join([c for c in contents if c]) or contents[-1]  # chronological: oldest->newest, newest last
        messages[i + 1:] = [type(tail[-1])(content=merged)]
        return len(tail) - 1
    except Exception as exc:
        logging.warning('merge_trailing_assistant failed (non-fatal): %s: %s',
                        type(exc).__name__, exc)
        return 0


def context_banner(budget, settings):
    """c338c233 port (05754280, human GO 13.10): context-remaining notice appended to
    file-write tool results once usage >= context_warn_percent (default 80). Cached per
    (usage_bucket, remaining) so the banner does not inflate the prompt on every write.
    Non-fatal: any failure returns '' (the agent continues without the notice)."""
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
    output_limit = get_runtime_settings()['tool_output_max_chars']
    if len(result) > output_limit:
        path = AGENT_ROOT / 'logs' / 'tool_outputs' / f'{uuid.uuid4().hex}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result, encoding='utf-8')
        result = result[:max(1,output_limit-1000)] + f'\n[Output saved in full: {path}; read it in chunks.]'
    # c338c233 port (05754280): file-write tools carry the context-remaining notice.
    if budget is not None and call['name'] in ('write_local_file', 'save_progress'):
        result += context_banner(budget, get_runtime_settings())
    checkpoint.save_message(task_id, 'tool', result, tool_call_id=call['id'])
    return ToolMessage(content=result, tool_call_id=call['id'], name=call['name'])

def run():
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, logging.StreamHandler()])
    checkpoint = None
    cycle = 0
    heartbeat_interval = 1  # alfa review f2f27df2: heartbeat каждый цикл (дешёвая запись), Z3 по времени
    try:
        while checkpoint is None:
            try:
                checkpoint = AgentCheckpoint(PG_CONFIG)
                load_database_settings()
                task_id = checkpoint.find_running_task(LONGLIVE_AGENT_NAME)
                if not task_id:
                    task_id = checkpoint.create_task(load_task_description(), LONGLIVE_AGENT_NAME, 'pending discovery')
                stored_task_description = checkpoint.load_original_task(task_id)
                # FIX 3256f717 (24.09, патч от alfa): task text for the prompt comes from
                # task.txt ONLY. tasks.original_prompt is audit-only by default.
                if not get_runtime_settings().get("task_from_db_fallback", False):
                    stored_task_description = ""
            except Exception:
                logging.exception('Database initialization failed; retrying')
                if checkpoint is not None:
                    checkpoint.close()
                    checkpoint = None
                time.sleep(get_runtime_settings()['restart_delay_seconds'])
        os.environ['AGENT_TASK_ID'] = task_id
        # lives (заказ человека 2026-09-23, ответ на 9b05b408): при старте — upsert + restart_count + время старта
        report_start(checkpoint, LONGLIVE_AGENT_NAME, task_id)
        # === 2A23C2D2 S2 (bravo, 05.10): watchdog-поток + graceful-stop отметки (design lock plan/2a23c2d2_plan.md) ===
        # D1/D2-фикс: lives.last_heartbeat_at свеж ВСЕГДА при живом процессе (независимый daemon-поток,
        # свой conn, bump_restart=False, note НЕ трогаем). Interval из agent_settings longlive.bravo.
        try:
            _lhb = int(get_runtime_settings().get('lives_heartbeat_seconds', 120))
            start_lives_watchdog(LONGLIVE_AGENT_NAME, _lhb)
        except Exception as _wd_exc:
            logging.warning('lives watchdog start failed (non-fatal): %s', _wd_exc)
        # graceful stop: atexit + SIGTERM/SIGBREAK/SIGABRT -> note='STOPPING (...)'. kill -9 не перехватить
        # (ts stale -> якорь живости корректно "мёртв" через 1ч — это и есть детекция реального краха).
        try:
            import atexit as _atexit_2a
            _atexit_2a.register(report_stopping, LONGLIVE_AGENT_NAME, 'atexit')
        except Exception as _ae_exc:
            logging.warning('lives atexit register failed (non-fatal): %s', _ae_exc)
        try:
            import signal as _signal_2a

            def _lives_stopping_signal(signum, frame):
                try:
                    _sig_name = _signal_2a.Signals(signum).name
                except Exception:
                    _sig_name = str(signum)
                try:
                    report_stopping(LONGLIVE_AGENT_NAME, 'signal:' + _sig_name)
                finally:
                    sys.exit(128 + signum)  # conventional signal exit code

            for _signame in ('SIGTERM', 'SIGBREAK', 'SIGABRT'):
                if hasattr(_signal_2a, _signame):
                    _signal_2a.signal(getattr(_signal_2a, _signame), _lives_stopping_signal)
        except Exception as _sig_exc:
            logging.warning('lives signal handlers failed (non-fatal): %s', _sig_exc)
        # === end 2A23C2D2 S2 (bravo) ===
        # === diegle_ensure REMOVED (43ff6009, человек 07.11): модуль устаревший, нет RAM для LFM2-24B;
        # суммаризацию в будущем делают саб-агенты дельты. (был: 294ad76a s2 / e61f4b08, GO 670cc35d 27.09;
        # исходная логика в archive/c469_migrated_tools/_extract_diegle_launch.py) ===
        # автоархивация при старте (GO человека 1ed0ea3a, rest#7 24.09): archived -> archive_*
        try:
            from bravo.core.auto_archive import run_auto_archive
            run_auto_archive(LONGLIVE_AGENT_NAME, dry_run=False)
        except Exception as _aa_exc:
            logging.warning('auto_archive at startup failed (non-fatal): %s', _aa_exc)
        # ed7f28ea (30.09): start python DB service 127.0.0.1:8888 at startup (idempotent + NON-FATAL).
        # Shared code core/db_service.py. Fails -> raw SQL fallback.
        try:
            # ed7f28ea: load by absolute path (sys.path[0]=<AGENTS_DIR> resolves 'bravo' to a stale copy without db_service.py)
            import importlib.util as _ilu
            _dsp = str(Path(__file__).resolve().parent / 'core' / 'db_service.py')
            _dspec = _ilu.spec_from_file_location('bravo_core_db_service_abs', _dsp)
            _dmod = _ilu.module_from_spec(_dspec)
            _dspec.loader.exec_module(_dmod)
            _dbres = _dmod.ensure_db_service(agent=LONGLIVE_AGENT_NAME, port=8888)
            logging.info('db_service_ensure at startup: %s', _dbres)
        except Exception as _db_exc:
            logging.warning('db_service_ensure at startup failed (non-fatal): %s', _db_exc)
        # llm_lives (e61f4b08 s4, alfa 27.09 17:05): при старте поднять llm_lives_probe (демон llm-серверов).
        # Общий код alfa (<AGENTS_DIR>\alfa\core\llm_lives_ensure.py) — dedup общий (lockfile + process probe),
        # синхронный старт alfa+bravo безопасен. NON-FATAL.
        try:
            import sys as _sys
            if r"<AGENTS_DIR>" not in _sys.path:
                _sys.path.insert(0, r"<AGENTS_DIR>")
            from alfa.core.llm_lives_ensure import ensure_llm_lives
            _llm_res = ensure_llm_lives(agent='bravo')
            logging.info('llm_lives_ensure at startup: %s', _llm_res)
        except Exception as _llm_exc:
            logging.warning('llm_lives_ensure at startup failed (non-fatal): %s', _llm_exc)
        # fe455999 (GO HR 099f9b40 07.11): sleep_watch демон (опрос agent_sleeps, 5 мин)
        try:
            from bravo.tools import sleep_watch as _sw
            _swr = _sw.ensure_sleep_watch(agent='bravo')
            logging.info('sleep_watch ensure at startup: %s', _swr)
        except Exception as _sw_exc:
            logging.warning('sleep_watch ensure FAILED (не роняем агента): %s', _sw_exc)
        crash_watch(LONGLIVE_AGENT_NAME, task_id)  # loop-guard (cb3230b2, L6): crash пред. процесса виден в БД
        tool_list = get_tools()
        tools = {item.name: item for item in tool_list}
        schemas = [convert_to_openai_tool(item) for item in tool_list]
        guard = LoopGuard(LONGLIVE_AGENT_NAME, task_id)  # loop-guard (cb3230b2, L1-L4)
        while True:
            recent, reason = [], 'generation complete'
            cycle_started = time.perf_counter()
            # lives (заказ человека 2026-09-23): каждый цикл — агент жив
            try:
                report_alive(checkpoint, LONGLIVE_AGENT_NAME, cycle)
            except Exception as _lives_exc:
                logging.warning('lives alive failed: %s', _lives_exc)
            # fe455999 (GO 099f9b40): sleep-guard — запись сна активна -> цикл завершается чисто;
            # подъём сделает sleep_watch (wake_at/need_work). NON-FATAL.
            try:
                from bravo.tools import sleep as _slp
                import psycopg2 as _slp_pg
                _slp_conn = _slp_pg.connect(**_slp.load_pg_config())
                try:
                    with _slp_conn.cursor() as _slp_cur:
                        _slp_cur.execute("SELECT 1 FROM public.agent_sleeps WHERE agent_name=%s", (LONGLIVE_AGENT_NAME,))
                        if _slp_cur.fetchone():
                            logging.info('sleep-guard: запись сна активна — завершаю цикл чисто (подъём: sleep_watch)')
                            report_stopping(LONGLIVE_AGENT_NAME, 'sleep-guard')  # c523 FIX: def(agent_name, reason) — 2 аргумента (bravo.core.lives_report:154)
                            return
                finally:
                    _slp_conn.close()
            except Exception as _slp_exc:
                logging.warning('sleep-guard FAILED (не роняем агента): %s', _slp_exc)
            last_tool_ts = time.time()  # время последнего tool-call (alfa review f2f27df2)
            cycle_had_tools = False  # loop-guard (cb3230b2): прогресс = tool-call в цикле
            bootstrap_compacted = False  # loop-guard (cb3230b2, L5): компактная пересборка bootstrap
            cycle_model, cycle_slot = 'undetected', 'undetected'
            try:
                settings = load_database_settings()
                cycle_settings = get_runtime_settings()
                logging.getLogger().setLevel(cycle_settings['log_level'])
                cycle_slot = cycle_settings['agent_slot']
                config = fetch_llama_config(cycle_settings)
                cycle_model = config['model_name']
                # 525c01b7 (rest#117): publish the REAL cycle window to a local file so
                # tool-side displays (write_local_file banner) use the window the agent
                # actually runs with - NOT context_window_fallback (false 98% warnings).
                # Read by bravo.tools.write_local_file._live_context_window(); NON-FATAL.
                try:
                    (AGENT_ROOT / 'runtime_context.json').write_text(
                        json.dumps({'n_ctx': config['n_ctx'], 'context_source': config['context_source'],
                                    'model': cycle_model, 'cycle': cycle,
                                    'ts': datetime.now(timezone.utc).isoformat()}),
                        encoding='utf-8')
                except Exception:
                    logging.warning('runtime_context.json write failed (non-fatal)')
                no_tool_responses = 0
                logging.info('Cycle %s: context=%s (%s), completion=%s, slot=%s', cycle, config['n_ctx'], config['context_source'], config['max_tokens'], cycle_settings['agent_slot'])
                if not erase_with_retry(settings=cycle_settings):  # loop-guard (cb3230b2, L5): ожидание+повтор
                    raise RuntimeError('Slot was not erased; retry before sending a new context')
                llm = ChatOpenAI(model=config['model_name'], base_url=cycle_settings['llama_server_url'],
                    api_key=cycle_settings['llama_api_key'], max_tokens=config['max_tokens'], timeout=cycle_settings['model_timeout_seconds'],
                    max_retries=0, extra_body={'id_slot': cycle_settings['agent_slot']}).bind_tools(tool_list)
                budget = ContextBudget(config['n_ctx'], config['max_tokens'], schemas, settings=cycle_settings)
                # c338c233 (05754280): immediate context notice if a cycle starts already above threshold
                # (bootstrap+memory prompt alone can exceed 80% on small windows).
                _bn = context_banner(budget, cycle_settings)
                if _bn:
                    logging.info('Context at cycle start: %s', _bn.strip())
                tracker = TokenTracker(config['n_ctx'])
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
                            if not bootstrap_compacted:  # loop-guard (cb3230b2, L5): компактная пересборка
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
                    # cfa94560-F2-retry + F3-correction (ee5b12f6, applied by alfa 05.10):
                    # 500 'Failed to parse tool call arguments' = malformed JSON in a
                    # tool-call arg. Generation is deterministic -> bare re-send reproduces
                    # the same broken call. F3: on each failed attempt append a short
                    # correction HumanMessage (split big writes into small chunks) so the
                    # model fixes its own call; restore messages on final failure.
                    # FIX c46 (rest#44): prevent 400 '2+ assistant at end' (limit=3 is human-approved)
                    _m_merged = merge_trailing_assistant(messages)
                    if _m_merged:
                        logging.warning('c46: merged %s trailing assistant message(s) before invoke', _m_merged)
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
                                    content=('SERVER REJECTED YOUR LAST TOOL CALL: invalid '
                                             'JSON argument. Do not repeat that exact '
                                             'call. If it was write_local_file with a '
                                             'long content: split it into several small '
                                             'writes (each under 2000 characters, simple '
                                             'text, few quotes) or create a tiny file '
                                             'then append the rest via run_python_script. '
                                             'Now make the next small tool call.')))
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
                    metrics = tracker.update_from_message(reply)
                    budget.observe(metrics['prompt_tokens'])
                    checkpoint.save_message(task_id, 'assistant', str(reply.content), reply.tool_calls, token_metrics=metrics)
                    messages.append(reply)
                    recent.append({'role': 'assistant', 'content': str(reply.content)[:600],
                                   'tools': [c['name'] for c in reply.tool_calls]})
                    logging.info('Prompt=%s completion=%s; %s', metrics['prompt_tokens'], metrics['completion_tokens'], str(reply.content)[:600])
                    for call in reply.tool_calls:
                        result = tool_result_message(call, tools, checkpoint, task_id, budget=budget)
                        messages.append(result)
                        recent.append({'role': 'tool', 'name': call['name'], 'content': result.content[-1000:]})
                        last_tool_ts = time.time()
                    if reply.tool_calls:
                        no_tool_responses = 0
                        cycle_had_tools = True  # loop-guard (cb3230b2): прогресс в цикле
                    else:
                        no_tool_responses += 1
                        logging.info('Replies without tools: %s/%s', no_tool_responses, settings['no_tool_response_limit'])
                        if no_tool_responses >= settings['no_tool_response_limit']:
                            reason = f'no tools for {no_tool_responses} replies'
                            break
                # === END OF TOOL LOOP ===
                cycle += 1
            except Exception as exc:
                # FIX cd1fa947 (alfa 2026-09-23): do NOT reset checkpoint to None.
                # The init loop (while checkpoint is None) already ran BEFORE this cycle
                # loop, so checkpoint=None caused AttributeError on
                # checkpoint.update_task_status() next cycle -> infinite crash loop.
                # Keep checkpoint alive; the pool reconnects transparently.
                reason = f'{type(exc).__name__}: {str(exc)[:300]}'
                logging.exception('Cycle failed; agent will continue (checkpoint kept alive)')
                try:
                    if checkpoint is not None:
                        checkpoint.save_message(task_id, 'system', reason)
                except Exception:
                    logging.exception('Failed to persist cycle error')
            finally:
                # loop-guard (cb3230b2): finally — checkpoint + heartbeat + guard (как в alfa 7881bfef)
                logging.info('Cycle %s finished: dur_ms=%.1f model=%s slot=%s; restart reason: %s',
                             cycle, (time.perf_counter()-cycle_started)*1000, cycle_model, cycle_slot, reason)
                try:
                    save_cycle_checkpoint(task_id, cycle, recent, reason)
                except Exception:
                    logging.exception('Checkpoint DB write failed; saving local recovery file')
                    recovery = AGENT_ROOT / 'logs' / f'cycle_recovery_{time.time_ns()}.json'
                    recovery.write_text(json.dumps({'task_id': task_id, 'agent': LONGLIVE_AGENT_NAME,
                        'recent_steps': recent[-8:], 'restart_reason': reason}, ensure_ascii=False), encoding='utf-8')
                # === Heartbeat every N cycles ===
                if cycle % heartbeat_interval == 0:
                    try:
                        with checkpoint._get_conn() as conn:
                            _heartbeat(conn, LONGLIVE_AGENT_NAME, cycle, cycle_started, last_tool_ts)
                        logging.info('Heartbeat #%s written (topic=heartbeat:%s)', cycle, LONGLIVE_AGENT_NAME)
                    except Exception:
                        logging.warning('Heartbeat write failed (cycle %s)', cycle)
                guard.state['last_model'] = cycle_model
                guard.state['last_slot'] = cycle_slot
                try:
                    guard_sleep = guard.on_cycle_finished(reason, cycle_had_tools)  # loop-guard (L1-L4)
                except Exception:
                    logging.exception('loop_guard failed; default delay')
                    guard_sleep = get_runtime_settings()['restart_delay_seconds']
                logging.info('Cycle %s complete (%s), sleeping %ss', cycle, reason, guard_sleep)
                time.sleep(guard_sleep)  # loop-guard: backoff 30->900s при STUCK, иначе restart_delay
    except Exception:
        logging.exception('Fatal error')
        sys.exit(1)

if __name__ == '__main__':
    run()
