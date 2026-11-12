# alfa — харнес долгоживущего агента-ОРКЕСТРАТОРА

> Детальный файл о конкретном агенте. Начните с [README.md](README.md) — там общий
> обзор, пять ключевых идей, кейсы и глоссарий. Здесь — устройство именно `alfa`.
> Канон (цикл, память, безопасность, якорь живости) — [harness_report.md](harness_report.md);
> здесь — только отличия alfa от общего каркаса. Секреты — плейсхолдеры
> «см. vault / agent_settings (БД)»; IP/порты — в `agent_settings`, сюда не выносятся.

## 1. Назначение и роль

`alfa` — ОРКЕСТРАТОР: инфраструктурный контур (Ubuntu-хост: LLM-серверы, BGE/RAG, VPN),
координация сабагентов delta1/2/3, gitlab/деплой, TG-валидатор (роли чатов
owner/project/guest; для project/guest — только рекомендации, write-запрет).
Работает на Windows-хосте, **циклами**: запуск → работа (tool-calls) → controlled
restart → снова, до явной остановки человеком.

## 2. Каталог

`<workdir>/main_workdir/alfa/`: `main.py` (ядро цикла), `core/` — 22 модуля (20 `.py`
+ 2 prompt-`.yaml`), `tools/` — 26 эталонных инструментов, `workspace/tools/<task_id>/`
(одноразовые, пишет сам), `archive/`, `logs/` (+ `tool_outputs/`), `progress/`
(JSON-чекпоинты), `keys/`+`secrets/` (**НЕ** копировать в GitHub), `task.txt`,
`run_alfa.bat`/`relaunch_alfa*.bat`, `sleep_watchdog_alfa.py` (демон спячки).
Иерархия: `core/`+`tools/` — эталон, только через ревью человека; полезный
workspace-инструмент после задачи предлагается «поднять в core» (реестр
`tools_registry`). Состав `core/` и `tools/` — таблица ниже (разница alfa от общего
каркаса: `tg_readonly_guard`, `ssh_helper`/`ssh_creds_resolver`, `settings_migration`).

| Модуль (core) | Назначение | Модуль (tools) | Назначение |
|---|---|---|---|
| `agent_checkpoint.py` | Чекпоинт-обёртка БД (task, save_message) | `query_postgres_local` | SQL (read-only 30с / write 120с) |
| `config.py` | PG-конфиг, runtime-настройки, пути | `bulk_insert_to_postgres`, `list_postgres_tables`, `get_table_schema` | INSERT / рекогносцировка |
| `longlive_memory.py` | «я» = последний чекпоинт (восстановление) | `read_local_file_chunked`, `write_local_file`, `list_files_in_directory` | Файлы (чанки, path-guard) |
| `llama_runtime.py` | ContextBudget, слоты GPU (fetch/erase) | `run_python_script` | Запуск Python (path-guard) |
| `loop_guard.py` | LoopGuard (L1–L6), crash_watch | `save_progress` | Чекпоинт + файл progress/ |
| `toolcall_validator.py` | K1: валидация tool-call (1 ретрай) | `request_restart` | Controlled restart (floor-отказ, §3) |
| `tg_readonly_guard.py` | Гвард TG-ролей (project/guest: read-only) | `sleep` / `sleep_watch` | Спячка (agent_sleeps) |
| `sql_safety.py` | allow_write-политика | `rag_search`, `embed_and_store` | RAG (BGE-M3 1024d, pgvector) |
| `lives_report.py` | report_start/alive/stopping + watchdog | `execute_ssh_command`(+`_home`), `ssh_execute_on_all_servers`, `ssh_creds_resolver` | SSH (креды из БД) |
| `rerank_client.py` | Клиент reranker (fallback vector-only) | `download_file_from_server`, `upload_file_to_server` | SFTP |
| `llm_lives_ensure.py` | Живость LLM-серверов (llm_lives) | `log_chunk_analyzer`, `batch_file_analyzer` | Батч-анализ через LLM (детач/status) |
| `db_failover.py`, `background_ensure.py`, `startup_archive.py`, `settings_migration.py`, `token_tracker.py`, `tools_registry.py`, `logged_tool.py`, `ssh_helper.py`, `check_deps.py` | Вспомогательные | `mailbox_send/read`, `tg_set_role`, `path_guard.py` | Почта, TG-роли, path-guard |

## 3. Логика инструментов (отличия alfa, на примерах из журналов)

Общий цикл и механизмы (ContextBudget, K1/F3, output bounding) — канон:
[harness_report.md](harness_report.md) §2. Ниже — как это выглядит в поведении alfa.

**Почему агент «не может уйти в рестарт» при пустом цикле (floor-отказ).**
`request_restart` проверяет контекст: если usage **ниже** `controlled_restart_threshold`
(деф. 80%, у alfa = 80, валидация 50–94) — рестарт **отклонён**, цикл продолжается;
добавлены условия: ≥3 tool-calls в цикле и свежий чекпоинт. Это защита от «побега в
рестарт»: модель не сбрасывает контекст, когда цикла ещё почти нет. Живые примеры из
`messages` (задача e874aa0e):

> «Рестарт отклонён (47.6% < 80%) — цикл продолжается. Есть что сделать: e2e-верификация draft-патча…»
> «Рестарт отклонён (76.4% < 80%) — цикл продолжается. Делаю полезную работу: быстрый терминальный срез…»

**Планировочный и жёсткий пороги** — floor/плановый = 80% (NOTE «контекст выше порога»
→ завершённый шаг = чекпоинт + рестарт), hard = 95% (safety net). Детали и три сценария
— [harness_report.md](harness_report.md) §2.

**LoopGuard** — детект зацикливания (повторяющиеся tool-calls): backoff, эскалация
cooldown, stop. Процедура при срабатывании — [how_we_work.md](how_we_work.md) §4.

**Output bounding** — вывод инструмента > лимита → полный вывод в
`logs/tool_outputs/<uuid>.txt`, в промпт — отсечка + путь (агент не «затягивает» окно).

**Чекпоинт и рестарт как пара** — `save_progress` пишет `analyst_daemon_queue`
(«я» = последний чекпоинт) + `progress/*.json`; controlled restart — чистый выход ПОСЛЕ
шага, новый процесс восстанавливает «я» из БД.

## 4. Якорь живости и спячка (специфика alfa)

`lives` (heartbeat + `restart_count`, у alfa на 11.11 = 205) обновляется кодом
автоматически; проверка живости напарника (>1ч) — [harness_report.md](harness_report.md)
и [how_we_work.md](how_we_work.md) §4. Спячка alfa — по `sleep.policy` из
`agent_settings` (человек разрешает длительные sleep, например 4h для проверки
peer-wake); sleep-guard чистым выходом завершает процесс, `sleep_watch` будит.

## 5. Безопасность (только отличия alfa)

Полная модель угроз — [harness_report.md](harness_report.md) §8. Специфика alfa:
TG-роли через `tg_readonly_guard` + `tg_set_role` (guest/project → read-only),
SSH-креды — только из реестра доступов БД (авторезолюция, в MD не пишутся),
SFTP без явных кредов не работает (авторезолюция — только для SSH-команд).

## 6. Уроки (из практики alfa)

- **Патч core-кода требует ПОЛНОГО перезапуска сервиса**: longlive-рестарт не re-exec'ит
  `config.py`; фикс в ядре не подхватится до полного рестарта процесса.
- **Prod-first для боевых контуров**: изменение логики боевого сервиса — сначала на
  проде (с бэкапом для отката), проверка человеком, потом пуш в разработку.
- **RAG активно**: карточка после каждого инцидента/решения/нетривиального факта
  (2-3+ за цикл), поиск через `rag_search` перед началом работы.
- **Дедуп сущностей, а не источников**: кейс «одна TG-сообщение → две задачи» — см.
  [README.md](README.md) §6.1.
- **SFTP без явных кредов не работает** (авторезолюция — только для SSH-команд);
  файлы на Linux-хост — SFTP с явным паролем (из реестра доступов) или base64-доставка.
