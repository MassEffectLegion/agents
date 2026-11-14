"""Long-lived recovery agent with SQLite memory and DENSE/MoE collaboration."""
import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langchain_core.messages import HumanMessage, ToolMessage
from omega.core.config import AGENT_ROOT, LOG_FILE, SQLITE_PATH, get_runtime_settings, reload_settings
from omega.core.instance_lock import InstanceLock
from omega.core.storage import SQLiteStore
from omega.core.memory import restart_prompt, save_cycle
from omega.core.models import ModelPool, set_active_pool
from omega.core.tools_registry import get_tools

def load_task():
    path = Path(os.environ.get('AGENT_TASK_FILE') or AGENT_ROOT / 'task.txt')
    return path.read_text(encoding='utf-8').strip() if path.exists() else ''

def bounded_output(value):
    text = str(value)
    limit = get_runtime_settings()['tool_output_max_chars']
    if len(text) <= limit:
        return text
    path = AGENT_ROOT / 'logs' / 'tool_outputs' / f'{uuid.uuid4().hex}.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return text[:limit-1000] + f'\n[Full output: {path}]'

def execute_tool(call, tools, store, task_id):
    try:
        result = tools[call['name']].invoke(call.get('args') or {})
    except Exception as exc:
        result = f'TOOL ERROR: {type(exc).__name__}: {exc}'
    result = bounded_output(result)
    # Persist evidence on disk as well, before a DB error can hide an executed side effect.
    journal = AGENT_ROOT / 'logs' / 'tool_events.jsonl'
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'task_id':task_id,'tool_call_id':call['id'],'tool':call['name'],'result':result},ensure_ascii=False)+'\n')
    store.save_message(task_id, 'tool', result, tool_call_id=call['id'])
    return ToolMessage(content=result, tool_call_id=call['id'], name=call['name'])

def inject_requests(store, task_id, messages, seen):
    for request in store.pending_requests():
        if request['request_id'] in seen:
            continue
        text = f"ПОРУЧЕНИЕ ЧЕЛОВЕКА {request['request_id']}:\n{request['request']}"
        messages.append(HumanMessage(content=text))
        store.save_message(task_id, 'user', text)
        store.mark_delivered(request['request_id'])
        seen.add(request['request_id'])

def run(max_cycles=None):
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(LOG_FILE,encoding='utf-8'),logging.StreamHandler()], force=True)
    with InstanceLock(str(SQLITE_PATH) + '.lock'):
        store = SQLiteStore()
        settings = reload_settings(store, force=True)
        if not any(server['enabled'] for server in settings['servers']):
            raise ValueError('No enabled models: fill servers in config.json')
        task_id, original_task = store.resume_or_create(load_task())
        os.environ['AGENT_TASK_ID'] = task_id
        tools_list = get_tools()
        tools = {tool.name:tool for tool in tools_list}
        cycle, preferred = 0, settings['primary_server']
        try:
            while max_cycles is None or cycle < max_cycles:
                recent, reason, actor_name = [], 'cycle interrupted', None
                try:
                    settings = reload_settings(store)
                    logging.getLogger().setLevel(settings['log_level'])
                    pool = ModelPool(settings)
                    set_active_pool(pool)
                    prompt = restart_prompt(store,task_id,load_task() or original_task,cycle)
                    messages = [HumanMessage(content=prompt)]
                    seen = set()
                    inject_requests(store,task_id,messages,seen)
                    if settings['council_on_cycle_start'] and len(pool.servers)>1:
                        advice = pool.consult('Предложи следующий проверяемый шаг восстановления по поручению и памяти. Сначала диагностика, затем изменения.',
                                              '\n'.join(str(m.content) for m in messages))
                        text = bounded_output(json.dumps(advice,ensure_ascii=False))
                        store.save_message(task_id,'advisor',text)
                        messages.append(HumanMessage(content='Независимые рекомендации моделей (не выполненные действия):\n'+text))
                    # Choose/fail over only before an inference. Tools execute once, outside this retry loop.
                    session = None
                    for name in pool.names(preferred):
                        try:
                            candidate = pool.session(name,tools_list)
                            reply = pool.invoke(candidate,messages)
                            session = candidate
                            break
                        except Exception:
                            logging.exception('Model %s unavailable for cycle %s; trying another server',name,cycle)
                    if session is None:
                        raise RuntimeError('All configured llama servers failed')
                    actor_name = session.name
                    no_tools = 0
                    store.save_message(task_id,'user',prompt)
                    while True:
                        metrics = getattr(reply,'usage_metadata',None) or {}
                        store.save_message(task_id,'assistant',str(reply.content),reply.tool_calls,token_metrics=metrics,model=actor_name)
                        messages.append(reply)
                        recent.append({'role':'assistant','content':str(reply.content)[-1500:],'tools':[c['name'] for c in reply.tool_calls]})
                        logging.info('Cycle=%s actor=%s prompt=%s budget=%s/%s response=%s',cycle,actor_name,
                                     session.budget.last_prompt,session.budget.limit,session.budget.context,str(reply.content)[:500])
                        for call in reply.tool_calls:
                            result = execute_tool(call,tools,store,task_id)
                            messages.append(result)
                            recent.append({'role':'tool','name':call['name'],'content':result.content[-1000:]})
                        recent = recent[-16:]
                        settings = reload_settings(store)
                        session.budget.update_policy(settings)
                        if reply.tool_calls:
                            no_tools = 0
                        else:
                            no_tools += 1
                            if no_tools >= settings['no_tool_response_limit']:
                                reason = f'no tool calls in {no_tools} consecutive replies'
                                break
                            save_cycle(store,task_id,cycle,recent,'continuing same context')
                            continuation = 'Продолжай с той же историей. Проверь поручения/оставшиеся шаги; не повторяй уже выполненные действия.'
                            messages.append(HumanMessage(content=continuation))
                            store.save_message(task_id,'user',continuation)
                            time.sleep(settings['restart_delay_seconds'])
                        inject_requests(store,task_id,messages,seen)
                        if not session.budget.can_call(messages):
                            reason = f'context reserve reached: prompt={session.budget.last_prompt}, limit={session.budget.limit}, window={session.budget.context}'
                            break
                        reply = pool.invoke(session,messages)
                    preferred = settings['primary_server']
                except KeyboardInterrupt:
                    reason = 'stopped by human'
                    raise
                except Exception as exc:
                    reason = f'{type(exc).__name__}: {str(exc)[:1000]}'
                    logging.exception('Cycle error; omega will continue')
                    if actor_name:
                        alternatives = [s['name'] for s in settings['servers'] if s['enabled'] and s['name']!=actor_name]
                        preferred = alternatives[0] if alternatives else actor_name
                finally:
                    save_cycle(store,task_id,cycle,recent,reason)
                    if cycle % 10 == 0:
                        try:
                            store.backup(AGENT_ROOT/'data/backups'/f'omega-{time.time_ns()}.sqlite3')
                        except Exception:
                            logging.exception('Automatic SQLite backup failed')
                    logging.info('Cycle %s ended: %s',cycle,reason)
                    cycle += 1
                if max_cycles is None or cycle < max_cycles:
                    time.sleep(get_runtime_settings()['restart_delay_seconds'])
        except KeyboardInterrupt:
            logging.info('Omega stopped by owner; background jobs remain available by job_id')
        finally:
            set_active_pool(None)
            logging.shutdown()

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--max-cycles',type=int,help='Bounded smoke run; omitted means infinite')
    args=parser.parse_args()
    run(args.max_cycles)
