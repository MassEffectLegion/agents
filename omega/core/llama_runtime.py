"""Llama context discovery and preflight budgeting for every model request."""
import json
import math
import requests
from langchain_core.messages import convert_to_openai_messages
from omega.core.config import get_runtime_settings


def estimate_tokens(value):
    # Shared fallback, calibrated against actual server usage in ContextBudget.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return math.ceil(len(text) / 3.5)

def _positive(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None

def fetch_llama_config(settings=None):
    runtime = dict(settings or get_runtime_settings())
    LLAMA_SERVER_URL = runtime['llama_server_url']
    LLAMA_API_KEY = runtime['llama_api_key']
    AGENT_SLOT = runtime['agent_slot']
    CONTEXT_WINDOW_TOKENS = runtime['context_window_fallback']
    COMPLETION_MAX_TOKENS = runtime['completion_max_tokens']
    CONTEXT_SAFETY_TOKENS = runtime['context_safety_tokens']
    result = {'model_name': runtime['model_fallback'], 'n_ctx': CONTEXT_WINDOW_TOKENS, 'context_source': 'config fallback'}
    base = LLAMA_SERVER_URL.removesuffix('/v1')
    headers = {'Authorization': f'Bearer {LLAMA_API_KEY}'}
    candidates = []
    for endpoint in ('/models', '/props', '/slots'):
        url = LLAMA_SERVER_URL + endpoint if endpoint == '/models' else base + endpoint
        try:
            response = requests.get(url, headers=headers, timeout=runtime['server_timeout_seconds'])
            response.raise_for_status()
            data = response.json()
            if endpoint == '/models':
                models = data.get('data') or data.get('models') or []
                if models:
                    model = models[0]
                    result['model_name'] = model.get('id') or model.get('name') or model.get('model') or runtime['model_fallback']
                    # Model metadata may describe training context, not allocated slot capacity.
                    meta_context = _positive((model.get('meta') or {}).get('n_ctx'))
                    if meta_context:
                        candidates.append((0, meta_context, 'models metadata'))
            elif endpoint == '/props':
                settings = data.get('default_generation_settings') or {}
                context = _positive(settings.get('n_ctx')) or _positive((settings.get('params') or {}).get('n_ctx'))
                if context:
                    candidates.append((1, context, 'props generation context'))
                elif _positive(data.get('n_ctx')):
                    parallel = _positive(data.get('total_slots')) or 1
                    candidates.append((1, int(data['n_ctx']) // parallel, 'props total context / slots'))
            else:
                slots = data if isinstance(data, list) else data.get('slots', [])
                for slot in slots:
                    if str(slot.get('id')) == str(AGENT_SLOT):
                        context = _positive(slot.get('n_ctx'))
                        if context:
                            candidates.append((2, context, 'selected slot'))
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            print(f'LLM discovery {endpoint}: {exc}')
    if candidates:
        _, result['n_ctx'], result['context_source'] = max(candidates, key=lambda item: item[0])
    result['max_tokens'] = min(COMPLETION_MAX_TOKENS, max(1, result['n_ctx'] // 4))
    if result['n_ctx'] <= result['max_tokens'] + CONTEXT_SAFETY_TOKENS:
        raise ValueError('Context is too small for completion and safety reserve')
    return result

def erase_llama_slot(slot_id=None, settings=None):
    runtime = dict(settings or get_runtime_settings())
    LLAMA_SERVER_URL = runtime['llama_server_url']
    LLAMA_API_KEY = runtime['llama_api_key']
    slot_id = runtime['agent_slot'] if slot_id is None else slot_id
    try:
        response = requests.post(LLAMA_SERVER_URL.removesuffix('/v1') + f'/slots/{slot_id}?action=erase',
                                 headers={'Authorization': f'Bearer {LLAMA_API_KEY}'}, timeout=runtime['server_timeout_seconds'])
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f'Slot erase failed: {exc}')
        return False

class ContextBudget:
    """Count the actual server chat template; reduce completion as the window fills."""
    def __init__(self, context, completion, schemas, settings=None):
        self.context, self.completion, self.schemas = context, completion, schemas
        runtime = dict(settings or get_runtime_settings())
        self.server_url = runtime['llama_server_url']
        self.api_key = runtime['llama_api_key']
        self.server_timeout = runtime['server_timeout_seconds']
        self.update_policy(runtime)
        self.use_tokenizer = True
        self.last_prompt = 0
        self.last_completion = 0
        self.source = 'unmeasured'
        self._correction = 0
        self._raw_count = 0

    def update_policy(self, settings):
        self.limit = int(self.context * settings['context_usage_percent'] / 100)
        self.safety = settings['context_safety_tokens']
        self.minimum_completion = settings['completion_min_tokens']
        self.completion = min(settings['completion_max_tokens'], max(1, self.context // 4))

    def _post(self, endpoint, payload):
        response = requests.post(self.server_url.removesuffix('/v1') + endpoint,
            headers={'Authorization': f'Bearer {self.api_key}'}, json=payload, timeout=self.server_timeout)
        response.raise_for_status()
        return response.json()

    def count(self, messages):
        # Send only OpenAI wire fields, not LangChain metadata/usage duplicated in model_dump().
        wire = {'messages': convert_to_openai_messages(messages), 'tools': self.schemas,
                'add_generation_prompt': True}
        self.use_tokenizer = False
        try:
            prompt = self._post('/apply-template', wire)['prompt']
            tokens = self._post('/tokenize', {'content': prompt, 'add_special': False,
                                               'parse_special': True})['tokens']
            self._raw_count = len(tokens)
            self.source = 'server chat template'
            self.use_tokenizer = True
            self.last_prompt = self._raw_count
        except (requests.RequestException, KeyError, TypeError, ValueError):
            # Retry endpoints on the next call: a transient timeout is not a permanent mode switch.
            payload = json.dumps(wire, ensure_ascii=False, default=str)
            self._raw_count = estimate_tokens(payload) + 16 * len(messages)
            self.source = 'usage-calibrated estimate' if self._correction else 'text estimate'
            self.last_prompt = max(0, self._raw_count + self._correction)
        return self.last_prompt

    def observe(self, prompt_tokens):
        # Actual prompt usage corrects fallback estimates, without summing repeated history.
        if prompt_tokens and self.source != 'server chat template':
            self._correction = int(prompt_tokens) - self._raw_count

    def can_call(self, messages):
        available = self.limit - self.count(messages) - self.safety
        self.last_completion = max(0, min(self.completion, available))
        # Keep a useful minimum response; do not restart merely because a full 8k no longer fits.
        return self.last_completion >= min(self.minimum_completion, self.completion)
