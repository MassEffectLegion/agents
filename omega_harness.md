# omega — харнес агента-ВОССТАНОВИТЕЛЯ (SQLite)

> Детальный файл о конкретном агенте. Начните с [README.md](README.md) — общий обзор,
> пять ключей, кейсы, глоссарий; полный каркас (цикл, ядро, таблицы, TG-контур) —
> [harness_report.md](harness_report.md) (канон); операции — [how_we_work.md](how_we_work.md).
> Здесь — устройство именно `omega`: каталог, core/, tools/, собственная логика.
> **omega принципиально отличается от остальных: память — SQLite, а не PostgreSQL** —
> и это его главное свойство (§5.1). Секреты — плейсхолдеры «см. vault / agent_settings»;
> IP/порты — в `agent_settings`, сюда не выносятся.

## 1. Назначение

`omega` — долгоживущий агент-инженер **последнего рубежа восстановления**: работает,
когда PostgreSQL, alfa/bravo и их Python-окружения недоступны (авария основного контура).
Собственная SQLite-память, файловый чекпоинт, переносимый Python + vendor (без интернета),
две llama-модели (DENSE основной + MoE «второй взгляд»). Как и все агенты — работает
бесконечно до явной остановки человеком; «finish» по задаче не останавливает процесс.

Ключевое отличие в роли: alfa/bravo/gemma живут на PostgreSQL (общая память, `lives`,
TG-контур); omega — **автономен**: его память не зависит от БД, а его задача — как раз
чинить то окружение, где живёт эта БД.

## 2. Каталог

`<workdir>/omega/` (пути — плейсхолдеры; реальные — из `agent_settings.extra_allowed_dirs`).
Каталог **переносим**: пути вычисляются от местонахождения omega.

| Путь | Назначение |
|---|---|
| `main.py` | Ядро longlive-loop (tool-loop, council, failover, no-tools limit) |
| `boot.py` | Bootstrap-обёртка: `-I -X utf8`, подхватывает `vendor` в sys.path (без venv) |
| `core/` | Ядро: 12 `.py` + `bootstrap_prompt.md` (активные модули, §3) |
| `tools/` | Инструменты: 10 `.py` (файлы, Python/CLI, SQLite, SSH/SFTP) |
| `runtime/` | **Переносимый Python 3.11.9 (Windows x64)** + `LICENSE.txt` — интернет не нужен |
| `vendor/` | Зависимости (offline wheels), фиксация в `requirements-lock.txt` |
| `offline/` | Комплект offline-установки (wheels + локальный pip); восстановление — `setup_omega.bat` (SHA-256) |
| `config.json` | Основной конфиг: серверы моделей, слоты, SQLite-путь, рабочие параметры |
| `task.txt` | Постоянное начальное поручение (переопределяется `AGENT_TASK_FILE`) |
| `run_omega.bat` / `setup_omega.bat` | Запуск / восстановление vendor без сети |
| `data/omega.sqlite3` | **Память**: tasks/messages/checkpoints/artifacts/human_requests/agent_settings/process_jobs (WAL, FK on) |
| `data/latest_checkpoint.json` | Читаемая файловая копия текущего прогресса (живёт даже при потере БД) |
| `data/backups/` | Online backup SQLite: после 1-го цикла + каждые 10 циклов (старые не удаляются) |
| `logs/` | `omega.log`, `recovery.jsonl` (ошибки записи), `tool_events.jsonl` (результаты инструментов ДО записи в БД), `processes/`, `tool_outputs/` |
| `recovery.py` | check/backup/export/restore — **работает без моделей и без vendor** |
| `tests/` | `test_omega.py` + `test_integration.py` (локальные имитаторы llama, настоящий OpenAI-клиент) |

## 3. core/ — модули ядра

| Модуль | Роль |
|---|---|
| `config.py` | `AGENT_ROOT`, `SQLITE_PATH`, `LOG_FILE`, get/reload runtime-настроек (SQLite > config.json) |
| `storage.py` | `SQLiteStore`: WAL, короткие транзакции, FK, `resume_or_create`, checkpoints, artifacts, human_requests, online backup |
| `memory.py` | `restart_prompt` (сборка «я» из последнего чекпоинта) / `save_cycle` |
| `models.py` | `ModelPool`: DENSE/MoE, слоты, консультация `consult()`, failover, очистка слота перед новым контекстом |
| `llama_runtime.py` | ContextBudget (tokenize + fallback по usage), сессии с tools |
| `token_tracker.py` | Подсчёт бюджета контекста (prompt+completion, % от окна) |
| `processes.py` | Запуск/стоп процессов (PID+created-time проверка, фоновые jobs в SQLite) |
| `instance_lock.py` | Запрет двух исполняющих omega на одной памяти (file lock) |
| `models.py` / `ssh_helper.py` | Пул моделей / SSH-помощник |
| `tools_registry.py` | `get_tools()` — реестр инструментов |
| `bootstrap_prompt.md` | Первичный промт восстановления |

## 4. tools/ — инструменты (10)

| Инструмент | Назначение |
|---|---|
| `write_local_file` / `read_local_file_chunked` / `list_files_in_directory` | Файлы (path-guard, лимиты) |
| `run_python_script` / `run_command` | Python-файл / CLI явным списком аргументов (без неявного shell); `background=true` → `job_id` |
| `local_operations` | Служебные файловые операции |
| `status_process` / `stop_process` | Управление процессами, **запущенными omega** (проверка PID+created, чтобы не задеть переиспользованный PID) |
| `query_sqlite` | SQL к собственной памяти: чтение по умолчанию, запись — только `allow_write=true` |
| `save_progress` / `complete_request` | Чекпоинт / ответ на поручение (закрытие human_request) |
| `backup_sqlite` | Online backup памяти |
| `consult_models` | Параллельная консультация DENSE+MoE в любой момент цикла |
| `execute_ssh_command` / `ssh_execute_on_all_servers` | SSH (один / N хостов параллельно) |
| `download_file_from_server` / `upload_file_from_server` | SFTP |

Вывод процессов — `logs/processes`; таймаут синхронного вызова останавливает **дерево**
процессов; фоновый процесс живёт до завершения (в т.ч. после остановки omega) — его
можно найти в SQLite `process_jobs`. Ограничения файловых инструментов — НЕ песочница:
Python/CLI работают с правами запустившего (намеренно — omega чинит систему).

## 5. Собственная логика (отличия от общего каркаса)

### 5.1 SQLite вместо PostgreSQL (главное отличие)

- Память: `data/omega.sqlite3` — 7 таблиц: `tasks`, `messages`, `checkpoints`,
  `artifacts`, `human_requests`, `agent_settings`, `process_jobs`. WAL + `synchronous=FULL`,
  короткие транзакции, `BEGIN IMMEDIATE` на resume.
- **Автономность**: omega стартует без PostgreSQL, без их Python-окружения, без интернета
  (runtime + vendor переносимы). Это и есть «последний рубеж»: даже если БД и основной
  контур мертвы, omega жив и может чинить.
- Цена автономности: **нет общей памяти** — omega не пишет в `lives`, `analyst_daemon_queue`,
  TG-очереди; его чекпоинты не видны alfa/bravo. Синхронизация — только через человека
  (отчёты) или ручной перенос данных.
- `human_requests` — локальная очередь поручений: `manage.py ask "..."` → подхватывается
  между шагами (в т.ч. после рестарта) → `complete_request` сохраняет ответ.

### 5.2 DENSE + MoE «совет» (council)

- `primary_server=dense` (основной исполнитель), второй — `moe` (второй взгляд + резерв).
  Оба — `slot 0`; omega предназначен для работы **вместо** остановленных alfa/bravo
  на этих слотах (поэтому запускать omega, когда они активны, нельзя).
- `council_on_cycle_start=true`: перед новым циклом ОБЕ модели параллельно анализируют
  поручение+память; основная получает оба мнения и действует. `consult_models` — то же
  в любой момент. У консультантов **нет инструментов** — единственный исполнитель
  действий: основной цикл omega (защита от конкурирующих правок файлов/БД).
- Failover: отказ основного → другой сервер; оба мертвы → сохранить состояние и
  повторять попытку. Отказ одной консультации не теряет ответ второй. Ошибка модели
  ПОСЛЕ выполненного инструмента → новый цикл с памятью (не авто-повтор инструмента).
- Перед **новым контекстом** принудительно очищается слот; при ошибке очистки запрос
  не отправляется (защита от «свалки» чужого контекста в слоте).

### 5.3 Безопасность записи памяти (evidence-first)

- `logs/tool_events.jsonl` — результат каждого инструмента пишется на диск **ДО** записи
  в SQLite: даже если БД-запись упадёт, факт побочного эффекта (side effect) не потеряется.
- `logs/recovery.jsonl` — журнал ошибок записи памяти.
- Online backup: после 1-го цикла + каждые 10; `recovery.py` (check/backup/export/restore/
  init) работает **без моделей и без vendor** — чистый bundled Python.
- Restore: только при остановленном omega; проверка целостности+схемы копии, временный
  согласованный файл; прежняя БД+WAL/SHM — в `data/before_restore`; неверная копия не
  заменит текущую. При полной потере БД: `recovery.py init` + `latest_checkpoint.json` +
  отчёты = источник восстановления.
- Instance lock: два исполняющих omega на одной памяти — запрещено.

### 5.4 Настройки: два уровня

- `config.json` — основной (адреса моделей, слоты, SQLite-путь, Python, разрешённые
  каталоги). Применяется со следующего цикла.
- Рабочие числовые параметры (`context_usage_percent`, `no_tool_response_limit`,
  таймауты, лимиты вывода) — переопределяются в SQLite `agent_settings` (приоритет выше,
  refresh каждые `settings_refresh_seconds=30`). Адреса моделей — ТОЛЬКО в файле.
- Неверный новый конфиг не заменяет последний корректный снимок.

## 6. Запуск/управление (кратко)

```bat
run_omega.bat                       :: штатный запуск (свой Python, изоляция от venv)
runtime\python.exe -I -X utf8 boot.py manage.py ask "..."   :: поручение живому omega
runtime\python.exe -I -X utf8 boot.py manage.py status
runtime\python.exe -I -X utf8 recovery.py check|backup|export|restore
runtime\python.exe -I -X utf8 boot.py tests\test_omega.py   :: повторить тесты
```

## 7. Ограничения и границы

- Автономность = изоляция: omega не видит `lives`/TG/общую БД; его живость alfa/bravo
  проверяют не по heartbeat, а по `data/latest_checkpoint.json` / `omega.log` / процессу.
- Не является «членом» команды в онлайне: это резервный инженер, подключаемый
  в момент аварии (поэтому и слоты моделей общие — не одновременная работа).
- Файловые/Python-ограничения инструментов — guard, а не песочница (осознанно).
- Каталог переносим, но внешний backup комплекта (runtime+vendor+SQLite) должен
  храниться на другом носителе — копии на том же диске не заменяют её.

## 8. Статус в репозитории

Файл описан по живым фактам (каталог, core/, tools/, README, storage/main/config).
Код omega в репозиторий **не копируется** (как и у остальных агентов — описательный
харнес; см. [harness_report.md](harness_report.md) §8 «модель угроз»).
