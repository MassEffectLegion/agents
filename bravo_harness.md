# bravo — харнес агента-ОПЕРАТОРА

> Детальный файл о конкретном агенте. Начните с [README.md](README.md) — общий обзор,
> пять ключей, кейсы, глоссарий; полный каркас (цикл, ядро, таблицы, TG-контур) —
> [harness_report.md](harness_report.md) (канон); операции — [how_we_work.md](how_we_work.md).
> Здесь — устройство именно `bravo`: каталог, core/, tools/, собственная логика
> инструментов, уроки. Секреты — плейсхолдеры «см. vault / agent_settings (БД)»;
> IP/порты — в `agent_settings`, сюда не выносятся.

## 1. Назначение

`bravo` — долгоживущий агент longlive (циклы: запуск → работа → рестарт → снова;
память — PostgreSQL). Роль по канону — **ОПЕРАТОР**: TG-мост (tg_bridge: приём
сообщений, вложения, авто-роутинг) + валидация TG-очереди (роли owner/project/guest),
деплой/инфраструктурные контуры, DR/бекап-киты, RAG-память, owner-контур
auto-archive и ротационных проверок. Как и все агенты — работает бесконечно до
явной остановки человеком; «finish» по задаче не останавливает процесс.

## 2. Каталог

`<workdir>/main_workdir/bravo/` (пути — плейсхолдеры; реальные каталоги — из
`agent_settings.extra_allowed_dirs`).

| Путь | Назначение |
|---|---|
| `main.py` | Ядро longlive-loop (bootstrap, tool-loop, controlled restart, watchdog, lives) |
| `core/` | Ядро: 17 `.py` + 2 `.yaml` (активные модули, §3). Изменение — только через ревью |
| `tools/` | Эталонные инструменты (26 активных, §4) + 3 relocated-файла устаревшего контура (откат) |
| `workspace/tools/` | Одноразовые скрипты цикла (`c<цикл>_*`); авто-архив переносит в `archive/` |
| `logs/` | Лог циклов + `tool_outputs/` (полные вырезы длинного вывода) + `cycle_recovery_*.json` |
| `progress/` | Артефакты save_progress (JSON, сжатая память) |
| `longlive_plan_<task8>.md` | План-файл цикла (перечитывается первым шагом после рестарта) |
| `run_bravo_guard.bat` | Запуск/рестарт (в т.ч. через guard-обёртку) |

## 3. core/ — модули ядра (17 `.py` + 2 `.yaml`)

| Модуль | Роль |
|---|---|
| `agent_checkpoint.py` | Чекпоинт-обёртка БД: find/create task, save_message, статусы |
| `bootstrap_prompt.yaml` / `system_prompt.yaml` | Первичный промт + системный промт |
| `config.py` | PG_CONFIG, runtime-настройки (agent_settings), allowed dirs (union Windows+POSIX) |
| `db_service.py` | **Свой middleware** agent↔PostgreSQL на :8888 — см. §5.2 (S1/S2, dogfood) |
| `lives_report.py` | report_start/alive/stopping + watchdog-поток (якорь живости, §5.4) |
| `llama_runtime.py` | ContextBudget (tokenize-подсчёт + байтовый fallback), fetch_llama_config, erase слота |
| `longlive_memory.py` | build_longlive_restart_prompt / write_checkpoint / load_last_checkpoint (+compact) |
| `loop_guard.py` | LoopGuard L1–L6 + crash_watch + erase_with_retry — см. §5.1 |
| `rerank_client.py` | Клиент реранкера (bge-reranker-v2-m3, fallback vector-only) |
| `sql_safety.py` | Политика allow_write (read-only по умолчанию для TG-ролей) |
| `ssh_helper.py` / `logged_tool.py` / `__init__.py` | SSH-помощник / обёртка инструментов / пакет |
| `token_tracker.py` | TokenTracker: текущий и пиковый % контекста (prompt+completion) |
| `tools_registry.py` | TOOL_NAMES — единый реестр инструментов (26) |

`.bak_*`-файлы рядом — бэкапы перед патчами (откат), в счёт не идут.

## 4. tools/ — эталонные инструменты (26)

| Инструмент | Назначение |
|---|---|
| `write_local_file` / `read_local_file_chunked` / `list_files_in_directory` | Файлы: запись, чтение чанками (лимит символов), списки; path-guard |
| `run_python_script` | Запуск `.py` (path-guard, timeout, лимит вывода, полный вывод в файл) |
| `save_progress` | Чекпоинт: файл `progress/` + запись `analyst_daemon_queue` (done/results/remaining) |
| `rag_search` / `embed_and_store` | RAG: поиск (косинус + теги + reranker) / `.md`-карточка → BGE-M3 → 1 вектор |
| `list_postgres_tables` / `get_table_schema` / `query_postgres_local` | Raw-SQL: рекогносцировка / schema+sample / SELECT-WITH-EXPLAIN (read 30с) или write (120с) |
| `db_api_query` / `db_api_health` / `db_api_write` | **Свой db_service :8888** (dogfood): read-only /health / insert+human_request — §5.2 |
| `bulk_insert_to_postgres` | Массовый INSERT (JSON-список) |
| `execute_ssh_command` / `execute_ssh_command_home` / `ssh_execute_on_all_servers` | SSH (авторезолюция кредов из реестра доступов БД; home — без явных кредов; параллельно на N хостов) |
| `ssh_creds_resolver` | Резолюция SSH-кредов (точный IP → CIDR) |
| `download_file_from_server` / `upload_file_to_server` | SFTP |
| `ask_vision` | Vision-модель (конфиг в agent_settings; честная ошибка при down) |
| `web_search` | Веб-поиск (только провайдеры из agent_settings) |
| `gemma_subagent` | Одноразовая подзадача на слабой модели (сведение больших выводов, экономия контекста) |
| `sleep` | Спячка: --sleep/--status/--need_work/--wake/--cancel + REFUSE-проверки — §5.3 |
| `sleep_watch` | Watchdog спячки (5 мин, FOR UPDATE): будит по wake_at или need_work |
| `f012_rotation_check` | Ротационная read-only-проверка alert-узлов (окно eDaily; вердикт harvesting OK/CHECK) |
| `path_guard.py` | Общий guard путей для файловых инструментов |

Отдельно в каталоге (не в TOOL_NAMES): `lfm2_digest_ctl*` + `lfm2_digest_ctl_README.md`
(.relocated) — устаревший контур суммаризации, убран из реестра, оставлен для отката;
`launch_bonsai_delta_llms_bat.bat` — запуск моделей дельт.

## 5. Собственная логика инструментов (отличия от общего каркаса)

Общий цикл и механизмы (ContextBudget, K1/F3, output bounding) — канон в
[harness_report.md](harness_report.md) §2–§3. Здесь — то, что специфично для bravo.

### 5.1 LoopGuard: защита от «живого, но бесполезного» агента

Bootstrap-луп: цикл падает (500 tool-call / слот / подключение), ядро глотает ошибку,
пишет heartbeat и перезапускает тот же цикл. По якорю живости (`lives`) агент ВИДИТСЯ
ЖИВЫМ, но не делает работы — напарник, глядя только на heartbeat, проблему не увидит.
Защита (все функции NON-FATAL):

| Уров. | Механизм |
|---|---|
| L1 | Счётчик no-progress циклов; «прогресс» = был tool-call в цикле ИЛИ normal-причина завершения; состояние в `logs/loop_state.json` (переживает рестарт ПРОЦЕССА) |
| L2 | Backoff сна между циклами 30→900с при застревании, сброс на прогресс |
| L3 | Фиксация: `agent_operations_log` (stage=error) + `lives.note='STUCK xN …'` — видно напарнику одним SELECT по якорю |
| L4 | Эскалация: запись в `human_request` (status=wait) с подсказкой по типу причины; дедуп по reason_key с cooldown |
| L5 | Self-heal: `erase_with_retry` (слот llama-server) + компактная пересборка bootstrap-промпта при oversized-памяти |
| L6 | `crash_watch` при старте процесса: error-причина последнего чекпоинта предыдущего процесса фиксируется в БД (виден краш, даже если напарник мёртв) |

Классификация причины (error/normal) устойчивая: normal-причины (context reserve
reached, no tools for N replies, generation complete, …) сбрасывают счётчик; error
(slot_not_erased, tool_json_500, bootstrap_oversized, db_error, connection) — нет.

### 5.2 db_service :8888 — middleware agent↔БД (собственный, S1/S2)

Flask-сервис в `core/db_service.py`, bind 0.0.0.0 (доступен с LAN, когда агента
«увезли» с хоста): `/query` (read-only: SELECT/WITH/EXPLAIN, 403 на write),
`/insert`, `/update`, `/human_request`, `/human_task_queue[/claim|/notes]`, `/mailbox`,
`/plans`, `/health`, `/self_stats`, `/tool_metrics`, `/context_usage`.

- Каждый ответ `/query` несёт `est_tokens` — оценка, сколько контекста занял ответ
  (человек меряет экономию против raw-SQL). `/tool_metrics` — per-route calls + est_tokens.
- Инструменты `db_api_*` ходят в этот сервис → dogfood: в `messages.tool_calls` видно
  ИМЯ инструмента, а не SQL; сервис — наблюдаемый путь доступа к БД.
- Политика: DB_API-FIRST — raw-SQL только то, что сервис не покрывает (DDL,
  специфичные update); fallback при down — `query_postgres_local` (NON-FATAL).

### 5.3 Sleep: REFUSE-проверки перед сном

`sleep --sleep <период>` отказывает (REFUSE), если: (a) нет свежего чекпоинта
(<30 мин) в `analyst_daemon_queue` — «сначала save_progress, потом сон»; (b) есть
непрочитанные TG (`tg_message_queue status='new'`). После успеха — запись
`agent_sleeps` (wake_at, reason, kind), `lives.note='SLEEPING until …'`,
`tasks.status='sleeping'`; процесс завершается ЧИСТО (sleep-guard в main), а
`sleep_watch` (каждые 5 мин, SELECT … FOR UPDATE) поднимает его в wake_at или досрочно
при `need_work=true`.

### 5.4 Якорь живости + watchdog

`report_start` (upsert, restart_count+1) при старте и `report_alive` каждый цикл;
свой daemon-поток бьёт heartbeat (интервал из agent_settings) независимо от цикла —
`lives.last_heartbeat_at` свежа ВСЕГДА при живом процессе (D1/D2-фикс: зависший
tool-loop не прячет живость от напарников).

### 5.5 Ограничение рестарта «полным контекстом» (floor, общий механизм)

Инструмент controlled restart (реализован в alfa/gemma; у bravo рестарт цикла делает
ядро само при исчерпании бюджета) нельзя вызвать «на всякий случай»:

```
if context_percent < floor (default 80, диапазон валидации 50–94): REFUSE
if tool_calls_this_cycle < min (default 3): REFUSE
if last checkpoint age > 30 min: REFUSE
```

Смысл: если контекст ещё свободен (<80%), агенту ЗАПРЕЩЕНО сбрасывать его —
рестарт отклонён, цикл продолжает полезную работу (пример: «Рестарт отклонён
(47.6% < 80%) — цикл продолжается»). Это снимает класс дёрга: «рестартик = новая
идея», и заставляет агента либо работать, либо спать (sleep c.5.3).

## 6. Уроки (из практики bravo)

- **«Жив» ≠ «работает»**: heartbeat — необходимое, но недостаточное. Счётчик
  no-progress + STUCK-метка в якоре — то, что делает застревание видимым (LoopGuard).
- **Output bounding — гигиена контекста**: длинный вывод (> лимита) — в файл,
  в промпт — отсечка + путь. Без этого один `netstat`/`psql` съедает окно.
- **RAG-first и NEVER-REVEAL**: карточка в RAG после каждого инцидента/решения;
  секреты живут в vault/agent_settings, в файлах и промптах — плейсхолдеры
  (урок c633d94b: 149 строк raw-секретов в .md-карточках пришлось переносить в vault).
- **Инфраструктурные грабли DR**: «off-site зеркало» требует учёта привилегий канала
  (remote sudo-rsync: share читается только через sudo), а WAL-архив — честного
  расчёта места (546GB архив, ~45GB/день): кит сначала считает, потом пишет (40cf28b0).
- **Честность в доках**: счётчики core/tools — по живому каталогу; если файл
  «планируется» — так и писать, а не ссылаться на несуществующий (P4, замечания v3).

## 7. Ограничения

- Работает на Windows-хосте; LLM-слот и BGE — на выделенных узлах (адреса —
  agent_settings). При их недоступности — деградация (fallback-подсчёт токенов,
  vector-only RAG), а не смерть: все зависимости NON-FATAL.
- Write в БД — только через свой контур/по явному GO; TG guest/project =
  рекомендация, не команда (write-интент из такого чата исполняется никогда).
- Код `core/`/`tools/` — эталон: изменения только через ревью человека; агент
  пишет одноразовое только в `workspace/`.
