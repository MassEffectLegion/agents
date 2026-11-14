# delta2 — рабочий роя (longlive)

Долгоживущий автономный рабочий. Напарник `delta1` в рое (один делает, второй ревью-критикует).
Память — PostgreSQL `agents`, BGE-M3 RAG, канал с человеком — `human_request`. По ядру близок к
delta1/alfa (общее `core/`).

## Структура

- `main.py` — основной цикл.
- `core/` — ядро: `config.py`, `bootstrap_prompt.yaml` / `system_prompt.yaml`,
  `longlive_memory.py`, `startup_archive.py`, `llama_runtime.py`, `loop_guard.py`,
  `token_tracker.py`, `sql_safety.py`, `db_failover.py`, `lives_report.py`, `rerank_client.py`,
  `tg_readonly_guard.py`.
- `tools/` — инструменты LLM: `query_postgres_local`, `db_api_query` / `db_api_write`
  (через db-API `:8888`), `execute_ssh_command`, `ssh_creds_resolver`, `rag_search`,
  `embed_and_store`, файловые, `save_progress`, `sleep`, `request_restart`.
- `run_delta2.bat` — запуск (Windows). `DB_SERVICE_URL` — адрес db-API.

## Запуск (Windows)

1. `pip install -r requirements.txt`
2. `runtime_settings.local.json` — креды PostgreSQL (шаблон: `runtime_settings.local.json.example`).
3. `runtime_defaults.json` — URL llama-server и BGE.
4. `task.txt` — поручение.
5. `run_delta2.bat` (лог: `logs\delta2.txt`).

## Linux

```sh
python3 -m pip install -r requirements.txt
export DB_SERVICE_URL=http://127.0.0.1:8888
python3 main.py
```

## Рой

delta1 и delta2 — два процесса над одной PostgreSQL. Координация — через `agent_coordination`
и `mailbox_*`. Запускайте оба, указав каждому свой `AGENT_SLOT`/лог-файл.
