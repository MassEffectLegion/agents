"""Compact restart prompts and recoverable checkpoints, entirely local."""
import json
import logging
from pathlib import Path
from .config import AGENT_ROOT

def save_cycle(store, task_id, cycle, recent, reason):
    payload = {'task_id':task_id,'agent':'omega','cycle':cycle,'restart_reason':reason,'recent_steps':recent[-8:]}
    try:
        saved = store.latest_checkpoint(task_id)
        saved.update(cycle=cycle, restart_reason=reason)
        if recent:
            saved['recent_steps'] = recent[-8:]
        payload = dict(saved,task_id=task_id,agent='omega')
        store.checkpoint(task_id, payload, reason)
    except Exception:
        logging.exception('SQLite checkpoint failed; writing local recovery journal')
        recovery = AGENT_ROOT / 'logs' / 'recovery.jsonl'
        recovery.parent.mkdir(parents=True, exist_ok=True)
        with recovery.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'task_id': task_id, 'cycle': cycle, 'recent_steps': recent[-8:], 'reason': reason}, ensure_ascii=False) + '\n')
    finally:
        # Independent readable checkpoint survives a broken SQLite file/library or missing model stack.
        path=AGENT_ROOT/'data/latest_checkpoint.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)

def restart_prompt(store, task_id, description, cycle):
    bootstrap = (AGENT_ROOT / 'core' / 'bootstrap_prompt.md').read_text(encoding='utf-8')
    payload = store.latest_checkpoint(task_id)
    file_memory = ''
    checkpoint = AGENT_ROOT / 'data' / 'latest_checkpoint.json'
    if not payload and checkpoint.exists():
        try:
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            file_memory = ('\nРЕЗЕРВНАЯ ПАМЯТЬ ИЗ ФАЙЛА (может относиться к прежней задаче; '
                           'сверь с текущим поручением и состоянием файлов):\n' +
                           json.dumps(saved, ensure_ascii=False)[:12000])
        except (OSError, ValueError):
            logging.exception('Cannot read fallback file checkpoint')
    memory = json.dumps(payload, ensure_ascii=False)
    if len(memory) > 12000:
        memory = memory[:12000] + '\n[Полный чекпоинт доступен через query_sqlite.]'
    recovery = AGENT_ROOT / 'logs' / 'recovery.jsonl'
    recovery_text = ''
    if recovery.exists():
        with recovery.open('rb') as handle:
            handle.seek(max(0, recovery.stat().st_size - 6000))
            recovery_text = '\nЛокальный журнал сбоев записи (сверь task_id/цикл):\n' + handle.read().decode('utf-8', errors='replace')
    return (bootstrap.replace('{agent_root}', str(AGENT_ROOT)) +
            f'\nЦикл: {cycle}\nЗадача: {task_id}\nИсходное поручение:\n{description}\n'
            f'ПАМЯТЬ SQLITE:\n{memory}\n{file_memory}\n{recovery_text}')
