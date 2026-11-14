# bravo — агент-оператор (longlive)

Долгоживущий автономный оператор: SSH к серверам, работа с PostgreSQL (через db-API `:8888`
с fallback на raw SQL), RAG (BGE-M3), web-поиск, TG-мост (ingest), подзадачи gemma/deltas.
Работает циклами (longlive), память — PostgreSQL `agents`.

## Структура

- `main.py` — основной цикл.
- `core/` — ядро: `config.py`, `bootstrap_prompt.yaml` / `system_prompt.yaml`,
  `longlive_memory.py`, `startup_archive.py`, `db_service.py` (клиент db-API `:8888`),
  `llama_runtime.py`, `loop_guard.py`, `token_tracker.py`, `sql_safety.py`, `lives_report.py`.
- `tools/` — инструменты LLM: `execute_ssh_command` (+ `_home`), `ssh_execute_on_all_servers`,
  `ssh_creds_resolver` (авторешение creds из БД), `db_api_health` / `db_api_query` / `db_api_write`,
  `query_postgres_local` (fallback raw SQL), `rag_search`, `embed_and_store`, `ask_vision`,
  `web_search`, `gemma_subagent`, `run_python_script`, `save_progress`, `sleep`,
  `run_bravo_guard.bat` (guard-процесс), `launch_bonsai_delta_llms_bat.bat` (запуск 2 CPU-LLM).

## Запуск (Windows)

1. `pip install -r requirements.txt`
2. `runtime_settings.local.json` — креды PostgreSQL (шаблон: `runtime_settings.local.json.example`).
3. `runtime_defaults.json` — URL llama-server и BGE.
4. `task.txt` — поручение.
5. `run_bravo.bat` (лог: `logs\bravo.txt`).

## Linux

```sh
python3 -m pip install -r requirements.txt
python3 main.py   # креды в runtime_settings.local.json или PG*
```

## Особенности

- SSH-креды: `tools/ssh_creds_resolver.py` авторешает доступ из БД (vault); в коде и логах
  секреты не хранятся (маскируются).
- db-API (`DB_SERVICE_URL`, порт 8888) — основной путь к БД; raw SQL — только fallback
  при UNREACHABLE.
- TG-мост (ingest) — отдельная сущность, запускается отдельно (не в `run_bravo.bat`).
