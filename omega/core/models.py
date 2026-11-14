"""Parallel read-only model advice, one tool executor, and server failover."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from .config import get_runtime_settings, server_runtime
from .llama_runtime import ContextBudget, fetch_llama_config, erase_llama_slot

@dataclass
class ModelSession:
    name: str
    model: object
    budget: ContextBudget
    runtime: dict
    detected: dict

class ModelPool:
    def __init__(self, settings=None):
        self.settings = settings or get_runtime_settings()
        self.servers = {s['name']: s for s in self.settings['servers'] if s['enabled']}
        self.detected = {}
        self._locks = {name: threading.Lock() for name in self.servers}

    def names(self, preferred=None):
        first = preferred or self.settings['primary_server']
        return sorted(self.servers, key=lambda name: (name != first, name))

    def session(self, name, tools=None, max_tokens=None, timeout=None):
        server = self.servers[name]
        runtime = server_runtime(server, self.settings)
        if name not in self.detected:
            self.detected[name] = fetch_llama_config(runtime)
        detected = self.detected[name]
        with self._locks[name]:
            if not erase_llama_slot(settings=runtime):
                raise RuntimeError(f'Slot erase failed on {name}; no model request sent')
        maximum = min(max_tokens or detected['max_tokens'], detected['max_tokens'])
        runtime['completion_max_tokens'] = maximum
        runtime['completion_min_tokens'] = min(runtime['completion_min_tokens'], maximum)
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schemas = [convert_to_openai_tool(tool) for tool in (tools or [])]
        budget = ContextBudget(detected['n_ctx'], maximum, schemas, settings=runtime)
        model = ChatOpenAI(model=detected['model_name'], base_url=runtime['llama_server_url'],
                           api_key=runtime['llama_api_key'], max_tokens=maximum,
                           timeout=timeout or runtime['model_timeout_seconds'], max_retries=0,
                           extra_body={'id_slot': runtime['agent_slot']})
        if tools:
            model = model.bind_tools(tools)
        return ModelSession(name, model, budget, runtime, detected)

    def invoke(self, session, messages):
        if not session.budget.can_call(messages):
            raise ValueError(f'Context budget reached on {session.name}: prompt={session.budget.last_prompt}, limit={session.budget.limit}')
        with self._locks[session.name]:
            reply = session.model.invoke(messages, max_tokens=session.budget.last_completion)
        usage = getattr(reply, 'usage_metadata', None) or {}
        session.budget.observe(usage.get('input_tokens'))
        return reply

    def consult(self, question, context='', names=None):
        selected = names or self.names()
        unknown = set(selected) - set(self.servers)
        if unknown:
            raise ValueError('Unknown or disabled servers: ' + ', '.join(sorted(unknown)))
        if not selected:
            return {'error': 'No enabled llama servers. Edit config.json.'}
        selected = list(dict.fromkeys(selected))
        def ask(name):
            try:
                session = self.session(name, max_tokens=self.settings['council_max_tokens'],
                                       timeout=self.settings['council_timeout_seconds'])
                message = HumanMessage(content=(
                    'Ты технический консультант omega. Только анализ: инструментов и права выполнять действия у тебя нет. '
                    'Дай проверяемый план, риски и диагностические команды; не утверждай, что уже выполнил их.\n'
                    + 'ВОПРОС:\n' + question + '\nКОНТЕКСТ:\n' + context))
                reply = self.invoke(session, [message])
                return {'ok': True, 'model': session.detected['model_name'], 'content': str(reply.content)}
            except Exception as exc:
                return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        # Both servers run independently and concurrently. One failure does not discard the other answer.
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            pending = {executor.submit(ask, name): name for name in selected}
            return {pending[future]: future.result() for future in as_completed(pending)}

_pool = None

def set_active_pool(pool):
    global _pool
    _pool = pool

def get_active_pool():
    return _pool or ModelPool()
