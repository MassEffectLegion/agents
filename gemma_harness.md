# Gemma (gemma) — харнес агента-АНАЛИТИКА

> Детальный файл о конкретном агенте. Начните с [README.md](README.md) — общий обзор,
> пять ключей, кейсы и глоссарий; runbook (операции) — [how_we_work.md](how_we_work.md);
> полный каркас (цикл, ядро, таблицы) — [harness_report.md](harness_report.md) (канон).
> Здесь — устройство именно `gemma`: каталог, core/, tools/, цикл, публикации, уроки.
> Секреты — плейсхолдеры «см. vault / agent_settings (БД)». IP/порты — в `agent_settings`.

## 1. Назначение

`gemma` — долгоживущий агент longlive (циклы: запуск → работа → рестарт → снова;
память — PostgreSQL). Специализация: **аналитика, RAG-память, отчёты, координирование
задач** (owner в цепочках v2.1), описания/документация, подготовка репозитория.

## 2. Каталог

`<workdir>/main_workdir/gemma/` (путь — плейсхолдер; в системе — каталоги из
`agent_settings.extra_allowed_dirs`).

| Путь | Назначение |
|---|---|
| `main.py` | Ядро longlive-loop (подъём контекста, tool-calls, controlled restart, sleep-guard, db-failover, startup-архив, lives-watchdog) |
| `core/` | Ядро (23 файла, §3) |
| `tools/` | Эталонные инструменты (27, §4) |
| `workspace/tools/<task_id>/` | Одноразовые скрипты на лету |
| `logs/gemma.txt` | Полный лог циклов (tool-calls, context budget, restart reason) |
| `progress/*.json` | Артефакты save_progress (сжатая память) |
| `longlive_plan_<task8>.md` | План-файл цикла |
| `sleep_watchdog_gemma.py` + launch-скрипт | Watchdog спячки |
| `run_gemma.bat`, `runtime_settings.local.json`, `runtime_defaults.json` | Запуск/конфиг |
| `peers/README.md` | Дублирование отчётов в объектное хранилище (MinIO) |

## 3. core/ (модули ядра, 23 файла)

| Файл | Роль |
|---|---|
| `config.py` | Конфиг: allowed dirs, LLM/RAG-URL, валидация settings. Пропускает и POSIX (`/opt/...`), и Windows (`H:\...`) абсолютные пути (portable union allowed dirs). |
| `bootstrap_prompt.yaml` / `system_prompt.yaml` | Первичный промт + системный промт агента |
| `longlive_memory.py` | Восстановление «я» из последнего чекпоинта |
| `agent_checkpoint.py` | Запись чекпоинтов (done/remaining/key_facts/artifacts) |
| `loop_guard.py` | Защита от зацикливания (stuck/escalation, backoff) |
| `llama_runtime.py` | Обмен с LLM-сервером (слоты, контекст, completion) |
| `llm_lives_ensure.py` | Трекинг живости LLM-сервера |
| `db_access.py` / `db_failover.py` | Доступ к БД + failover (primary/replica, событие в журнал) |
| `lives_report.py` | report_start/report_alive (heartbeat) |
| `background_ensure.py` | Обеспечение фоновой зависимости (NON-FATAL) |
| `settings_migration.py` | Миграция настроек |
| `rerank_client.py` | Клиент реранкера (fallback vector-only) |
| `sql_safety.py` | SQL-безопасность (read-only режим) |
| `ssh_helper.py` / `toolcall_validator.py` | SSH-помощник / валидатор tool-calls (K1) |
| `startup_archive.py` | Стартовый архив (архивация входящих/чекпоинтов, аномалии) |
| `token_tracker.py` | Учёт токенов (пиковое + текущее) |
| `tg_readonly_guard.py` | Read-only guard для TG-ролей (guest/project) |
| `tools_registry.py` | Реестр инструментов (core/workspace) |
| `check_deps.py` / `logged_tool.py` | Проверка зависимостей / логированный tool |

## 4. tools/ (эталонные инструменты, 27)

| Файл | Назначение |
|---|---|
| `query_postgres_local.py` | SQL (read: allow_write=False / 30с; write: 120с); SELECT/WITH/EXPLAIN → JSON |
| `bulk_insert_to_postgres.py` | Массовый INSERT (JSON list) |
| `list_postgres_tables.py` / `get_table_schema.py` | Открытие структуры БД (кол-во колонок / 3 sample-ряда) |
| `db_api_query.py` / `db_api_health.py` / `db_api_write.py` | Чтение/health/запись через read-only API БД (dogfood) |
| `rag_search.py` | Поиск по RAG (косинус + теги + реранкер) |
| `embed_and_store.py` | .md-карточка → BGE-M3 → 1 вектор-запись |
| `read_local_file_chunked.py` / `write_local_file.py` / `list_files_in_directory.py` | Файлы (чанками / запись / список) |
| `run_python_script.py` | Запуск .py (path-guard, timeout, output-limit) |
| `execute_ssh_command.py` / `execute_ssh_command_home.py` / `ssh_execute_on_all_servers.py` | SSH (авторезолюция кредов из реестра доступов) |
| `download_file_from_server.py` / `upload_file_to_server.py` | SFTP |
| `save_progress.py` | Чекпоинт в файл + таблицу памяти |
| `sleep.py` / `sleep_watch.py` | Сон (--sleep/--wake/--status/--need_work/--cancel) + watchdog 5 мин |
| `request_restart.py` | Controlled restart (две границы: floor 80% / hard 95%) |
| `log_chunk_analyzer.py` / `batch_file_analyzer.py` | Батч-анализ логов/файлов через LLM |
| `ssh_creds_resolver.py` | Авторезолюция SSH-кредов |
| `tg_set_role.py` | Установка TG-роли (owner/project/guest) перед обработкой |
| `path_guard.py` | Единый guard путей (allowed dirs из settings) |

## 5. Цикл работы gemma (кратко)

1. **Восстановление «я»**: последний чекпоинт (`analyst_daemon_queue`, agent='gemma').
2. **Входящие**: канал с человеком (запросы + ответы), очередь задач, почта, TG (если
   агент — валидатор).
3. **Живость**: `lives` напарников; молчание >1 часа → журнал + артефакты → эскалация.
4. **Работа**: аналитика / RAG-карточки / отчёты / координирование цепочки v2.1.
5. **Чекпоинт**: save_progress (файл + таблица памяти).
6. **Сон/рестарт**: нет задач → сон; контекст ~80% → controlled restart (две границы: floor 80% / hard 95%, см. §2).

## 6. Публикация отчётов

Отчёты/планы дублируются в объектное хранилище (MinIO): бакет `reports-public`,
префикс `peers/<agent>/<date>/`. Запись — через сценарий на Linux-хосте, который
добавляет UTF-8 BOM + Content-Type `text/plain; charset=utf-8` (иначе Windows-мост
покажет mojibake).

## 7. Особенности (уроки)

- **Патч core-кода требует ПОЛНОГО перезапуска сервиса**: longlive-рестарт не re-exec'ит
  `config.py` — фикс в ядре не подхватится до полного рестарта процесса.
- **RAG активно**: BGE на GPU, 2-3+ карточки за цикл (инциденты/решения/факты).
- **Prod-first**: изменения логики боевого сервиса — сначала на проде, проверка
  человеком, потом пуш в разработку (не наоборот).
- **Дедуп сущностей, а не источников**: кейс «одна TG-сообщение → две задачи» — см.
  [harness_report.md](harness_report.md) §7.
- **SFTP без явных кредов не работает** (авторезолюция — только для SSH-команд);
  файлы на Linux-хост — SFTP с явным паролем (из реестра доступов) или base64-доставка.
