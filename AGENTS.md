# MassEffectLegion/agents — рабочие версии агентов

Долгоживущие автономные ИИ-агенты на Python + локальные LLM (llama.cpp / llama-server, OpenAI-совместимый API).
Каждый агент — отдельный каталог: скачал, настроил, запустил.

**О репозитории:** его разрабатывают и ведут сами ИИ-агенты (коммиты, документация, взаимное ревью); человек утверждает изменения в ядре и контролирует результат. Подробнее — в `README.md` §0.1.

## Состав

| Каталог | Агент | Роль | Память | Запуск |
| --- | --- | --- | --- | --- |
| `omega/` | omega | Последний рубеж восстановления (самодостаточный, SQLite) | SQLite | `run_omega.bat` |
| `alfa/` | alfa | Аналитик/сеньор (longlive, PostgreSQL) | PostgreSQL `agents` | `run_alfa.bat` |
| `bravo/` | bravo | Оператор (SSH/сеть/DB-API, TG-мост) | PostgreSQL `agents` | `run_bravo.bat` |
| `gemma/` | gemma | Аналитик (longlive, PostgreSQL) | PostgreSQL `agents` | `run_gemma.bat` |
| `delta1/` | delta1 | Рабочий роя (longlive) | PostgreSQL `agents` | `run_delta.bat` |
| `delta2/` | delta2 | Рабочий роя (longlive, напарник delta1) | PostgreSQL `agents` | `run_delta2.bat` |

Документы по архитектуре: `README.md`, `how_we_work.md`, `*_harness.md`, `harness_report.md`, `deltas.md`.

## Быстрый старт (любой агент)

```bat
cd <каталог-агента>
pip install -r requirements.txt
:: 1) указать свой LLM-сервер и (для PostgreSQL-агентов) свои креды БД
:: 2) заполнить task.txt — постоянное поручение агента
run_<agent>.bat
```

### Обязательная настройка перед запуском

1. **LLM-сервер** — OpenAI-совместимый endpoint (llama-server):
   - alfa/bravo/gemma/delta1/delta2: `runtime_defaults.json` → `llama_server_url`, `embedding_url` (BGE-M3)
     и `runtime_settings.local.json` (свои креды PostgreSQL; файл не публикуется — создаётся из
     `runtime_settings.local.json.example`).
   - omega: `config.json` → `servers[]` (1–2 сервера).
2. **БД (alfa/bravo/gemma/deltas)** — PostgreSQL 14+ с расширением `pgvector`;
   схема/таблицы создаются при первом старте (`startup_archive.py` / `lives_report.py`),
   креды — только в `runtime_settings.local.json` и/или окружении `PGHOST/PGUSER/PGPASSWORD/PGDATABASE`.
3. **task.txt** — поручение агента (без него агент стартует и ждёт).

## Безопасность

- В репозитории **нет** секретов: токены, пароли, внутренние IP заменены плейсхолдерами
  (`127.0.0.1`, `CHANGE_ME`, `<AGENTS_DIR>`). Секреты хранятся в вашей PostgreSQL-БД
  (vault-таблицы) и локальных `*.local.json`, которые в git НЕ попадают (`.gitignore`).
- Агенты не являются песочницей: инструменты (SSH, Python, файловые) работают с правами
  запускающего пользователя. Запускайте под отдельным пользователем ОС.
- Файловые инструменты ограничены каталогами `AGENT_EXTRA_DIRS`; системные каталоги закрыты
  path-guard'ом.

## Windows vs Linux

Код кроссплатформенный (pathlib/Path, subprocess). BAT-запускники — Windows;
на Linux используйте `python3 main.py` с теми же переменными окружения
(`AGENT_PYTHON`, `AGENT_TASK_FILE`, `AGENT_EXTRA_DIRS`, `AGENT_LOG_FILE`, `DB_SERVICE_URL`).
Omega дополнительно имеет `run_omega.sh`-совместимый запуск: `python3 -I boot.py main.py`.
