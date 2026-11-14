# gemma — агент-аналитик (longlive)

Долгоживущий автономный аналитик. Работает циклами (longlive), память — PostgreSQL `agents`,
RAG — pgvector + BGE-M3, канал с человеком — `human_request`, цепочки задач — `human_task_queue`.
По архитектуре близок к alfa (общее ядро `core/`), отличия — свои промпты и задачи.

## Структура

- `main.py` — основной цикл.
- `core/` — ядро: `config.py`, `bootstrap_prompt.yaml` / `system_prompt.yaml`,
  `longlive_memory.py`, `startup_archive.py`, `llama_runtime.py`, `loop_guard.py`,
  `token_tracker.py`, `sql_safety.py`, `db_failover.py`, `lives_report.py`, `rerank_client.py`,
  `tg_readonly_guard.py`.
- `tools/` — инструменты LLM: `query_postgres_local`, `rag_search`, `embed_and_store`,
  `execute_ssh_command`, файловые, `save_progress`, `sleep` (+ `sleep_watchdog_gemma.py`),
  `request_restart`, `log_chunk_analyzer`, `batch_file_analyzer` и др.

## Запуск (Windows)

1. `pip install -r requirements.txt`
2. `runtime_settings.local.json` — креды PostgreSQL (шаблон: `runtime_settings.local.json.example`).
3. `runtime_defaults.json` — URL llama-server и BGE.
4. `task.txt` — поручение.
5. `run_gemma.bat` (лог: `logs\gemma.txt`).

## Linux

```sh
python3 -m pip install -r requirements.txt
python3 main.py
```

## Настройки (ключевые)

`context_usage_percent` (95); `no_tool_response_limit` (2); `restart_delay_seconds` (5);
`extra_allowed_dirs` — каталоги файловых инструментов. Валидация в `core/config.py`.
