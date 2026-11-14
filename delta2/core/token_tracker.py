"""

TokenTracker — отслеживание использования токенов.



Отслеживает токены каждого шага агента.

Метрика get_context_usage_percent() — это процент токенов prompt 

последнего запроса к модели от размера контекста.

Для пикового использования контекста используйте get_peak_context_usage_percent().

"""

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

import json

from delta2.core.llama_runtime import estimate_tokens





class TokenTracker:

    """Tracks agent token usage and context utilization."""



    def __init__(self, max_context_tokens: int = 65000):

        self.max_context_tokens = max_context_tokens

        self.reset()



    def reset(self):

        """Reset all counters."""

        self.total_prompt_tokens = 0

        self.total_completion_tokens = 0

        self.total_tokens = 0

        self.step_count = 0

        self.last_prompt_tokens = 0

        self.last_completion_tokens = 0

        self.history = []  # Per-step history



    def update_from_message(self, message) -> dict:

        """Extract and update token metrics from an AI message response.



        FIX #1: total_prompt_tokens теперь хранит ПИКОВОЕ (max) значение,

        а не сумму по всем шагам. В режиме CONTINUE каждый следующий prompt

        содержит всю предыдущую историю, поэтому суммирование приводило бы

        к многократному подсчёту одних и тех же токенов (двойной/тройной

        подсчёт одних и тех же сообщений истории).



        FIX #3: Fallback-оценка разделена по типам сообщений:

        - AIMessage: контент — это completion_tokens (ответ модели)

        - HumanMessage/ToolMessage: контент вносит вклад в prompt_tokens

        Эвристика изменена с //3 на //4 (ближе к реальности ~4 символа/токен).

        """

        metrics = {

            "prompt_tokens": 0,

            "completion_tokens": 0,

            "total_tokens": 0,

            "step": self.step_count

        }



        if hasattr(message, 'response_metadata') and message.response_metadata:

            metadata = message.response_metadata

            # Universal parser for Ollama AND llama.cpp

            # Ollama uses prompt_eval_count/eval_count

            # llama.cpp (via OpenAI API) uses prompt_tokens/completion_tokens

            metrics["prompt_tokens"] = (

                metadata.get('prompt_eval_count') or  # Ollama

                metadata.get('prompt_tokens') or      # llama.cpp / OpenAI

                metadata.get('token_usage', {}).get('prompt_tokens', 0)  # fallback

            )

            metrics["completion_tokens"] = (

                metadata.get('eval_count') or         # Ollama

                metadata.get('completion_tokens') or  # llama.cpp / OpenAI

                metadata.get('token_usage', {}).get('completion_tokens', 0)

            )

            metrics["total_tokens"] = metrics["prompt_tokens"] + metrics["completion_tokens"]



            # Timing (llama.cpp has timings in usage)

            if 'timings' in metadata:

                metrics["eval_duration_ms"] = metadata['timings'].get('predicted_ms', 0)

                metrics["total_duration_ms"] = metadata['timings'].get('prompt_ms', 0) + metrics["eval_duration_ms"]

            else:

                metrics["total_duration_ms"] = metadata.get('total_duration', 0) // 1_000_000

                metrics["eval_duration_ms"] = metadata.get('eval_duration', 0) // 1_000_000



        usage = getattr(message, 'usage_metadata', None) or {}

        if usage:

            metrics['prompt_tokens'] = usage.get('input_tokens', metrics['prompt_tokens'])

            metrics['completion_tokens'] = usage.get('output_tokens', metrics['completion_tokens'])

            metrics['total_tokens'] = metrics['prompt_tokens'] + metrics['completion_tokens']



        # Fallback for messages without metrics

        if metrics["total_tokens"] == 0 and hasattr(message, 'content'):

            # FIX #3: //4 вместо //3, корректное распределение по типам сообщений

            estimated = max(1, estimate_tokens({'content': message.content, 'tool_calls': getattr(message, 'tool_calls', None)}))

            msg_type = type(message).__name__

            if msg_type == 'AIMessage':

                # Контент AI-ответа — это completion_tokens, а не prompt

                metrics["completion_tokens"] = estimated

                metrics["prompt_tokens"] = 0

            else:

                # HumanMessage / ToolMessage: вносят вклад в prompt

                metrics["prompt_tokens"] = estimated

                metrics["completion_tokens"] = 0

            metrics["total_tokens"] = metrics["prompt_tokens"] + metrics["completion_tokens"]

            metrics["estimated"] = True



        # Update counters

        self.last_prompt_tokens = metrics["prompt_tokens"]

        self.last_completion_tokens = metrics["completion_tokens"]



        # FIX #1: peak-based tracking вместо cumulative sum

        # В режиме CONTINUE каждый prompt содержит всю историю.

        # Суммирование = двойной/тройной подсчёт одних и тех же токенов.

        # Храним максимальное значение для корректной оценки пикового использования.

        self.total_prompt_tokens = max(self.total_prompt_tokens, metrics["prompt_tokens"])



        # total_completion_tokens аккумулируем — каждый completion уникален (ответ модели)

        self.total_completion_tokens += metrics["completion_tokens"]

        self.total_tokens = self.total_prompt_tokens + self.total_completion_tokens



        self.step_count += 1

        self.history.append(metrics)

        return metrics



    def get_context_usage_percent(self) -> float:

        """Процент использования контекста токенами последнего запроса.



        ⚠️ Эта метрика отражает размер prompt-токенов последнего запроса к модели,

        а не суммарное использование контекста. Для режима CONTINUE, где история

        накапливается, она растёт — но это не означает накопительное использование.



        Именно эту метрику правильно использовать для is_approaching_limit().

        """

        if self.max_context_tokens > 0:

            return ((self.last_prompt_tokens + self.last_completion_tokens) / self.max_context_tokens) * 100

        return 0.0



    def get_peak_context_usage_percent(self) -> float:

        """Пиковое использование контекста за все шаги.



        В отличие от get_context_usage_percent(), эта метрика показывает

        максимальный размер prompt, который когда-либо использовался.

        Это более корректно для режима CONTINUE, где история накапливается.

        """

        if self.max_context_tokens > 0:

            return (self.total_prompt_tokens / self.max_context_tokens) * 100

        return 0.0



    def get_total_prompt_tokens_percent(self) -> float:

        """Устаревший метод — теперь эквивалентен get_peak_context_usage_percent().



        В предыдущей версии суммировал prompt-токены всех шагов, что приводило

        к завышенным значениям (>100%) в режиме CONTINUE.

        """

        return self.get_peak_context_usage_percent()



    def is_approaching_limit(self, threshold_percent: float = 75.0) -> bool:

        """Check if last request prompt exceeds threshold."""

        return self.get_context_usage_percent() >= threshold_percent



    def get_report(self) -> str:

        """Generate token usage report."""

        usage_percent = self.get_context_usage_percent()

        peak_usage_percent = self.get_peak_context_usage_percent()

        return f"""

=== TOKEN REPORT ===

Steps executed: {self.step_count}

Last request prompt: {self.last_prompt_tokens} tokens

Last request completion: {self.last_completion_tokens} tokens

Peak prompt tokens (max context usage): {self.total_prompt_tokens}

Total completion tokens (all steps): {self.total_completion_tokens}

Context usage (last request): {usage_percent:.1f}% of {self.max_context_tokens}

Context usage (peak): {peak_usage_percent:.1f}% of {self.max_context_tokens}

Status: {"⚠️ APPROACHING LIMIT" if self.is_approaching_limit() else "✅ NORMAL"}

"""

