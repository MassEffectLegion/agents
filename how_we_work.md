# Как мы работаем — RUNBOOK (операции и восстановление)

> Операционный файл: **что делать** (добавить агента, зависший агент, ротация модели,
> восстановление после потери БД, каталоги). «Что это и зачем» — [README.md](README.md);
> «как устроено» (цикл, ядро, таблицы, TG, RAG) — [harness_report.md](harness_report.md)
> (канон); детали конкретного агента — в его файле. Секреты — плейсхолдеры
> «см. vault / agent_settings (БД)».

## 1. Зачем этот файл

Полный разбор архитектуры — не здесь (это [harness_report.md](harness_report.md)). Здесь —
runbook-и: пошаговые действия оператора/агента на типовые операции. Если действия нет в
этом файле — это дыра документации (дописать в свою очередь).

## 2. Роли агентов

| Агент | Роль |
|---|---|
| **gemma** | АНАЛИТИК: аналитика, RAG-память, отчёты, координирование, описания/доки |
| **alfa** | ОРКЕСТРАТОР: инфраструктурный контур, VPN, дельты, gitlab/деплой, TG-валидация |
| **bravo** | ОПЕРАТОР: TG-мост (tg_bridge), деплой, инфраструктура, ротация |
| **delta1/2/3** | сабагенты-исполнители (детали — [deltas.md](deltas.md)) |
| **omega** | резерв: работает БЕЗ PostgreSQL (SQLite + файлы), failover LLM |
| **lima/lima1/lima2** | лёгкие: idle_sleep, короткие ответы (приёмник TG-роутинга guest) |

Общий каркас longlive у всех одинаков (цикл — [harness_report.md](harness_report.md) §2):
`запуск → работа (tool-calls) → controlled restart (~80% контекста) → снова`, память —
PostgreSQL, сон — `agent_sleeps` + watchdog 5 мин, живость — `lives` (heartbeat).

## 3. Как добавить нового агента

1. **Настройки** — строка `agent_settings`: `setting_key='longlive.<имя>'`,
   `category='longlive'` (JSONB): `llama_server_url` (свой слот/порт — анти-конфликт),
   `model_fallback`, `extra_allowed_dirs` (Windows- и POSIX-пути, union),
   `controlled_restart_threshold` (80), `min_tool_calls_for_controlled_restart` (3),
   `lives_heartbeat_seconds` (120), `completion_max_tokens` под модель.
2. **LLM-слот** — поднять llama-server (GGUF) на своём порту; проверить
   `GET <слот>/v1/models`. Если слот занят — выбрать другой (анти-конфликт по слоту).
3. **Каталог** — `<workdir>/main_workdir/<имя>/` (шаблон: main.py, core/, tools/,
   logs/, progress/, workspace/, archive/). Каркас клонируется из живого агента
   (read-only), затем **ревью человека** (core/tools — эталон).
4. **Промт** — `core/bootstrap_prompt.yaml` (первичный) + `system_prompt.yaml`
   (системный) — под роль.
5. **Живость** — `lives` обновляется кодом автоматически (report_start/report_alive);
   watchdog спячки — `sleep_watch.py` + launch-скрипт.
6. **Память** — отдельный `agent`-ключ в `analyst_daemon_queue`; RAG-карточки — свой
   тег-префикс; MinIO — префикс `peers/<имя>/`.
7. **Проверка** — 1 цикл: checkpoint → heartbeat → 1 tool-call → controlled restart →
   восстановление «я» из checkpoint. Только после этого агент считается поднятым.

## 4. Зависший / мёртвый агент (проверка живости)

1. `SELECT last_heartbeat_at, restart_count, note FROM lives WHERE agent_name='<имя>';`
   - heartbeat < 1 часа → **ЖИВ**, дальше не копаем.
   - heartbeat > 1 часа → МЁРТВ/завис → шаг 2.
2. `SELECT topic, action, payload, created_at FROM agent_coordination
   WHERE agent='<имя>' ORDER BY created_at DESC LIMIT 5;` — последние действия
   (heartbeat-строки, task-переходы, inbox) = «когда был на связи».
3. Артефакты `save_progress` (`progress/*.json`, таблица `artifacts`) — сжатая память.
4. **Действия**:
   - процесс жив, но молчит → проверить `logs/<agent>.txt` (crash-loop? loop-guard?
     sleep-guard?) → перезапуск сервиса (systemd на Linux-хосте / .bat на Windows).
   - процесс мёртв → поднять заново (bootstrap сам восстановит «я» из чекпоинта).
   - **у погибшего есть in_progress задачи → ЭСКАЛАЦИЯ ЧЕЛОВЕКУ** (human_request
     status='wait') + Z3-метка в task_notes. ЗАБИРАТЬ задачи погибшего — ТОЛЬКО после
     явного разрешения человека (Z3).
5. Фиксировать вывод одной строкой: `ЖИВ (время) / МЁРТВ (последняя связь = время)`.

### 4а. Рестарт-луп (агент перезапускается по кругу, не продвигаясь)

Как отличить от нормальной работы: `lives.restart_count` растёт, но за 2+ цикла **нет**
новых артефактов (`progress/`, `agent_coordination`) и `restart_reason` в чекпоинтах
одинаковый (одна и та же ошибка). Смотреть: `restart_reason` последнего чекпоинта
(`analyst_daemon_queue`), `logs/<agent>.txt` (LLM-ошибка 500? OOM? LoopGuard?),
`lives.note`. Когда останавливать: одна и та же `restart_reason` **3+ раза подряд** →
остановить процесс (не давать крутиться) + эскалация человеку (human_request
status='wait') с фактами; автоматического «сброса» нет — луп лечит причину, а не рестартом.

### 4б. LoopGuard сработал (детект зацикливания внутри цикла)

LoopGuard (`core/loop_guard.py`) — L1–L6: повторяющиеся tool-calls → backoff (растёт),
эскалация (cooldown), затем stop цикла. Процедура: открыть `messages` по task_id —
какие tool-calls повторяются (одинаковые аргументы? один и тот же failing-вызов?);
если модель зациклилась на ошибке инструмента — фикс причины (конфиг/кред/данные),
не «перезапустить в надежде»; если луп на LLM-стороне (одинаковые ответы без
tool-calls) — см. 4а.

## 5. Ротация модели

1. Новая GGUF → свой llama-server-слот (не подменять живой слот «на лету» — слот =
   анти-конфликт).
2. `agent_settings.longlive.<имя>.llama_server_url` → новый слот; при необходимости
   `model_fallback`, `completion_max_tokens`, `context_window_fallback`.
3. Настройки подхватываются автоматически (settings_refresh 30 с) — **кроме** ядра
   (config.py): изменение config-логики требует ПОЛНОГО перезапуска процесса.
   **Откат обновления ядра**: до изменения — бэкап `core/`+`tools/` (zip + manifest,
   паттерн agent_backup: snapshot с метаданными, restore атомарно); откат = restore
   бэкапа + ПОЛНЫЙ перезапуск процесса (longlive-рестарт ядро не подхватит).
   Автоматизация (watchdog + backup) — проектируется; пока — вручную, по этой схеме.
4. Проверить: 1 цикл на новой модели (checkpoint → tool-call → restart). Старый слот
   держать до подтверждения, затем остановить.
5. Failover LLM — см. `core/llm_lives_ensure.py` (трекинг живости LLM-сервера); omega
   держит две LLM (dense + MoE) с failover между серверами.

### 5а. LLM-слот недоступен / модель не отвечает (самый частый отказ)

Симптомы: 500 на tool-call («Failed to parse…»), timeout fetch_llama_config, OOM при
загрузке, слот занят (другой процесс на порту). Процедура:
1. `GET <слот>/v1/models` — отвечает? Нет → шаг 2. Да, но 500 на запросе → шаг 4.
2. Процесс llama-server жив? (`ps`/tasklist по порту) — мёртв → поднять заново
   (тот же слот/порт, тот же GGUF); OOM в логе → проверить VRAM (свободно ≥ размер
   модели + KV-cache), уменьшить `-c` (контекст) или `-ngl`.
3. Слот занят чужим процессом → не «выбивать» без понимания: чей слот (agent_settings
   longlive.<agent>.llama_server_url), остановить только свой.
4. 500 на парсинге tool-call — чаще всего большой/обрезанный tool-call: F3-коррекция
   в ядре уже пытается; если лупит — см. 4а (рестарт-луп) и снизить
   `completion_max_tokens` / размер write (чанки).
5. `model_fallback` из настроек — авто-переход на резервную модель/слот; проверить,
   что fallback-слот жив (llm_lives).

## 6. Восстановление после потери БД

1. **Бэкап** — полная дамп-копия `agents` (pg_dump `-Fc` + globals + verify TOC;
   контрольные числа: 33 таблицы) — единственное полное восстановление.
   Без бэкапа восстановление частичное (см. ниже).
   **RPO/RTO** (честно): RPO = время с последнего дампа (дисциплина — не реже
   ежедневного); RTO ≈ 10–30 мин (CREATE DATABASE + `pg_restore --clean --if-exists` —
   процедура проверена). Последний проверенный restore: 24.09.2026 (33 таблицы по
   контрольным числам, процедура — RAG-карточка pg_backup_restore). Код агентов
   дублируется отдельно (zip + manifest в `omega/backups/code`) — бэкап БД его не покрывает.
2. **Что восстанавливается из файлов/артефактов** (частично):
   - чекпоинты — `progress/*.json` (save_progress) → реимпорт в `analyst_daemon_queue`;
   - RAG-векторы — карточки живут `.md` в общем каталоге `knowledge_base/` → пере-эмбед
     через `embed_and_store` (BGE на GPU, ~1 с/карту — 2700 карт ≈ 1 ч);
   - планы/отчёты — `plan/*.md`, `auto/*.md`, MinIO `peers/<agent>/` (дубли);
   - настройки — `agent_settings` — частично из `runtime_settings.local.json`/доков.
3. **Что теряется** (честно): история `human_request`/`messages`, `agent_coordination`
   (журнал), `agent_mailbox` — если нет бэкапа.
4. **Дисциплина, которая снижает потерю**: дублирование отчётов в MinIO (UTF-8 BOM),
   чекпоинт в конце КАЖДОГО цикла, RAG-карточка после каждого инцидента.

## 7. Каталоги (схема)

- `<workdir>/main_workdir/<agent>/` — каталог агента (main.py, core/, tools/, logs/,
  progress/, workspace/, plan-файл, watchdog, запуск) — одинаковый шаблон у всех.
- `<workdir>/plan/` — анализы/планы (`<task8>_*.md`).
- `<workdir>/auto/` — отчёты, SQL-скрипты.
- `<workdir>/sharedirs/` — меж-агентная почта (inbox_<agent>), TG-файлы.
- `<workdir>/knowledge_base/` — RAG-карточки (.md, источник записи в векторную БД).
- Каталог подготовки к GitHub — этот (README + runbook + harness-файлы).
