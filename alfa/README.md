# alfa — агент-аналитик (longlive)

Долгоживущий автономный аналитик/сеньор. Работает циклами: запуск → работа → рестарт (сброс
контекста) → снова. Память — PostgreSQL (`agents`), восстановление «я» из последних чекпоинтов
(`analyst_daemon_queue`), канал с человеком — `human_request`, цепочки задач — `human_task_queue`,
RAG — pgvector + BGE-M3 (`embedding_url`).

## Структура

- `main.py` — основной цикл (LLM + инструменты, longlive-протокол).
- `core/` — ядро: `config.py` (настройки+дефолты), `bootstrap_prompt.yaml` / `system_prompt.yaml`
  (промпты), `longlive_memory.py` (чекпоинты), `startup_archive.py` (создание схемы/таблиц),
  `llama_runtime.py` (LLM-клиент), `loop_guard.py`, `token_tracker.py`, `sql_safety.py`,
  `db_failover.py`, `lives_report.py` (живость агентов), `startup_archive.py`,
  `rerank_client.py`, `tg_readonly_guard.py`.
- `tools/` — инструменты LLM: `query_postgres_local`, `rag_search`, `embed_and_store`,
  `execute_ssh_command`, `read_local_file_chunked`, `write_local_file`, `save_progress`,
  `sleep`, `sleep_watchdog_alfa.py`, `request_restart`, `log_chunk_analyzer` и др.
- `sleep_waiter.py` / `sleep_watchdog_alfa.py` — спячка агента (по GO человека) и guard.

## Запуск (Windows)

1. `pip install -r requirements.txt`
2. `runtime_settings.local.json` — свои креды PostgreSQL (шаблон см. `runtime_settings.local.json.example`).
3. `runtime_defaults.json` — URL вашего llama-server (порт 8080 по дефолту) и BGE (порт 9090).
4. Заполнить `task.txt` (постоянное поручение).
5. `run_alfa.bat` (лог: `logs\alfa.txt`).

## Linux

```sh
python3 -m pip install -r requirements.txt
export AGENT_PYTHON=$(which python3)
# креды БД: runtime_settings.local.json или PGHOST/PGUSER/PGPASSWORD/PGDATABASE
python3 main.py
```

## Настройки (ключевые)

`context_usage_percent` (95) — доля окна модели; `no_tool_response_limit` (2) — после двух
ответов без инструментов — новый цикл; `restart_delay_seconds` (5); `extra_allowed_dirs` —
каталоги для файловых инструментов. Все параметры валидируются (`core/config.py`),
некорректное значение не заменяет последний рабочий снимок.
