# -*- coding: utf-8 -*-
"""
batch_file_analyzer.py v2 — батч-анализ файлов через LLM + SLEEP-ПАТТЕРН (delta2, 16.10)
v2 по замечанию человека (bac70ef9, 16.10): инструмент предназначен для больших
каталогов (тысячи файлов) — агент НЕ ждёт синхронно:

  1. агент: save_progress (чекпоинт) + пометка в свою память "read <out>/final_report.md";
  2. агент: запускает инструмент с --detach -> процесс python ДЕТАЧИРОВАН, агент немедленно
     завершает цикл (рестарт/сон);
  3. процесс в фоне: читает файлы -> LLM per-file (reports/*.md) -> final_report.md -> WAKEUP.md;
  4. агент (следующий цикл): читает свой чекпоинт, `--status` -> FINAL_REPORT=yes ->
     read final_report.md -> отчёт/HR.

Аналог саб-агента: основной агент экономит контекст (файлы не проходят через его LLM-цикл).

ИСПОЛЬЗОВАНИЕ:
  python batch_file_analyzer.py --dir "H:\\path" --out "H:\\out" --detach          # sleep-паттерн
  python batch_file_analyzer.py --out "H:\\out" --status                           # 1 строка статуса
  python batch_file_analyzer.py --dir ... --out ... [--resume]                     # синхронный прогон

РЕЖИМЫ v2:
  --detach           запустить процесс DETACH-ом (переживёт завершение вызывающего) и выйти.
                     Пишет detached.json {pid, started_at}. Агент: чекпоинт -> рестарт (сон).
  --status           печатает 1 JSON-строку: total/done/failed/final_report/finished + tail run.log.
                     exit 0 = в работе, exit 10 = final_report.md готов (будильник для агента).
  --list / --dir     как в v1 (--list приоритетнее).
  --resume           продолжить с того же --out (обработанные пропускаются по progress.json).

ВЫХОД (в --out):
  reports/<relpath>.md   — отчет по КАЖДОМУ файлу
  progress.json          — {done, failed, total, started_at, finished_at, last_file} — resumable
  final_report.md        — ИТОГОВЫЙ сводный отчёт (все per-file -> LLM)
  WAKEUP.md              — «будильник» для проснувшегося агента (результат + что делать)
  detached.json          — pid/время detached-запуска
  run.log                — лог (stdout дочернего процесса тоже сюда)

LLM — OpenAI-совместимый /chat/completions. Default: 127.0.0.1:8080 (Qwen), api_key=local.
Один LLM-slot: при sleep-паттерне конфликта нет (агент спит). Для синхронных коротких прогонов
--max-files (не забивать slot, пока агент работает).
"""
import argparse, json, os, sys, time, traceback
from pathlib import Path
try:
    import requests
except ImportError:
    sys.exit("Нужен requests: pip install requests")

# ---------- конфиг (можно переопределить флагами) ----------
LLM_URL = "http://127.0.0.1:8080/v1"   # OpenAI-совместимый
LLM_MODEL = "Qwen"
API_KEY = "local"
REQUEST_TIMEOUT = 300
MAX_RETRIES = 3
RETRY_BACKOFF = 10

DEFAULT_EXT = {".py", ".java", ".kt", ".kts", ".xml", ".json", ".yaml", ".yml",
               ".sql", ".properties", ".gradle", ".txt", ".md", ".toml", ".cfg", ".conf"}

SYS_PROMPT = ("Ты — независимый код-ревьюер. Анализируй ОДИН файл за раз. "
              "Выдавай СТРОГО: (1) Назначение файла 1-2 строки; (2) Найденные ошибки/баги/риски "
              "с указанием строки/фрагмента (если нет — «не найдены»); "
              "(3) Рекомендации по исправлению (патч/конфиг) с приоритетом P0/P1/P2. "
              "Без воды, на русском, максимум ~40 строк.")

USER_TMPL = """Анализируй файл {path} из проекта {proj}.
Контент (может быть усечён до {n} символов):
```
{content}
```
"""

FINAL_TMPL = """Ты — старший аналитик. Ниже — сводные отчёты по {n} файлам проекта {proj}
(по одному отчёту на файл, от младшего ревьюера). Сформируй ИТОГОВЫЙ отчёт:
1) Топ проблем проекта (P0/P1/P2) с перечнем затронутых файлов;
2) Системные паттерны ошибок (что повторяется от файла к файлу);
3) План устранения: конкретные шаги, приоритеты, что можно чинить пачкой;
4) Что НЕ требует действий (ложные срабатывания/мелочь).
На русском, структурированно.

=== ОТЧЁТЫ ===
{reports}
"""

# exit-код "будильника" для --status: final_report готов
EXIT_FINAL_READY = 10


def log(out_dir, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(str(out_dir), "run.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def collect_files(args):
    files = []
    if args.list:
        with open(args.list, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    files.append(Path(ln))
    elif args.dir:
        root = Path(args.dir)
        exts = {e if e.startswith(".") else "." + e for e in args.ext} or DEFAULT_EXT
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", ".idea", "build", "out", ".gradle", "node_modules", "__pycache__")]
            for fn in filenames:
                if Path(fn).suffix.lower() in exts:
                    files.append(Path(dirpath) / fn)
    return files


def load_progress(out_dir):
    p = os.path.join(str(out_dir), "progress.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"done": {}, "failed": {}}


def save_progress(out_dir, prog):
    tmp = os.path.join(str(out_dir), "progress.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(str(out_dir), "progress.json"))


def llm_chat(url, model, api_key, messages, timeout=REQUEST_TIMEOUT):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url + "/chat/completions",
                              headers={"Authorization": "Bearer " + api_key},
                              json={"model": model, "messages": messages,
                                    "temperature": 0.2, "max_tokens": 2500},
                              timeout=timeout)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            content = msg.get("content")
            if content is None or not str(content).strip():
                # thinking-модели могут отдать ответ в reasoning* при пустом content
                content = msg.get("reasoning") or msg.get("reasoning_content")
            if content is None or not str(content).strip():
                raise ValueError("empty LLM content (модель вернула пустой ответ)")
            return content
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            log(".", f"LLM retry {attempt} after {wait}s: {e}")
            time.sleep(wait)


def analyze_file(url, model, api_key, fpath, proj, max_chars):
    try:
        text = fpath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            text = fpath.read_text(encoding="cp1251", errors="ignore")
        except Exception as e:
            return f"ОШИБКА ЧТЕНИЯ: {e}"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [УСЕЧЕНО, всего {len(text)} символов]"
    user = USER_TMPL.format(path=str(fpath), proj=proj, content=text, n=len(text))
    return llm_chat(url, model, api_key,
                    [{"role": "system", "content": SYS_PROMPT},
                     {"role": "user", "content": user}])


def final_report(url, model, api_key, out_dir, proj, max_chunk=24000):
    reports_dir = os.path.join(str(out_dir), "reports")
    entries = []
    for dirpath, _, filenames in os.walk(reports_dir):
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                p = Path(dirpath) / fn
                entries.append((p, p.read_text(encoding="utf-8", errors="ignore")))
    entries.sort(key=lambda x: x[0].name)
    parts = []
    cur, cur_len = [], 0
    for p, txt in entries:
        head = f"### {p.name}\n"
        body = txt[:1500]
        if cur_len + len(head) + len(body) > max_chunk and cur:
            parts.append("".join(cur)); cur, cur_len = [head + body], len(head) + len(body)
        else:
            cur.append(head + body); cur_len += len(head) + len(body)
    if cur:
        parts.append("".join(cur))
    final_text = ""
    for i, part in enumerate(parts, 1):
        user = FINAL_TMPL.format(n=len(entries), proj=proj, reports=part)
        if len(parts) > 1:
            user = user.replace("Сформируй ИТОГОВЫЙ отчёт",
                                f"ЧАСТЬ {i}/{len(parts)} отчётов. Формируй ЧАСТНЫЙ итог по этой части")
        out = llm_chat(url, model, api_key,
                       [{"role": "system", "content": "Ты — старший аналитик ПО."},
                        {"role": "user", "content": user}], timeout=600)
        final_text += (f"\n\n=== ЧАСТЬ {i}/{len(parts)} ===\n" if len(parts) > 1 else "") + out
    fp = os.path.join(str(out_dir), "final_report.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(final_text)
    return fp


def write_wakeup(out_dir, prog):
    """WAKEUP.md — будильник для проснувшегося агента (sleep-паттерн)."""
    ok, fail = len(prog["done"]), len(prog["failed"])
    total = prog.get("total", ok + fail)
    fr = os.path.join(str(out_dir), "final_report.md")
    text = f"""# WAKEUP — batch_file_analyzer завершён (sleep-паттерн)

- finished_at: {time.strftime('%Y-%m-%d %H:%M:%S')}
- итог: ok={ok} fail={fail} total={total}
- final_report.md: {"ГОТОВ — прочитать" if os.path.exists(fr) else "НЕТ (ok=0 или ошибка финала)"}

## Что делать агенту (проснувшемуся):
1. Прочитать этот файл (ты его нашёл по чекпоинту).
2. Прочитать final_report.md (каталог: {out_dir}).
3. По результату — отчёт в human_request / continuation задачи.
4. Если fail>0 и нужно — перезапустить с --resume (те же --out).
"""
    with open(os.path.join(str(out_dir), "WAKEUP.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return os.path.join(str(out_dir), "WAKEUP.md")


def status_view(out_dir):
    """-> (dict, exit_code): 0 = в работе/нет данных, 10 = final_report.md готов."""
    out_dir = str(out_dir)
    prog = load_progress(out_dir)
    ok, fail = len(prog["done"]), len(prog["failed"])
    fr = os.path.join(out_dir, "final_report.md")
    final = os.path.exists(fr)
    tail = []
    rl = os.path.join(out_dir, "run.log")
    if os.path.exists(rl):
        with open(rl, "r", encoding="utf-8", errors="ignore") as f:
            tail = [l.strip() for l in f.readlines()[-5:]]
    view = {
        "out": out_dir,
        "total": prog.get("total"),
        "done": ok,
        "failed": fail,
        "final_report": fr if final else None,
        "finished_at": prog.get("finished_at"),
        "last_file": prog.get("last_file"),
        "wakeup": os.path.exists(os.path.join(out_dir, "WAKEUP.md")),
        "tail": tail,
    }
    return view, (EXIT_FINAL_READY if final else 0)


def spawn_detached(args):
    """--detach: детач-запуск того же скрипта. Дочерний переживает выход родителя.
    Возвращает строку-сводку (pid, out)."""
    import subprocess
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # аргументы дочернего = текущие, без --detach/--quiet, + --detached-child
    child = [a for a in sys.argv[1:] if a not in ("--detach", "--quiet", "--status")]
    child += ["--detached-child"]
    cmd = [sys.executable, os.path.abspath(__file__)] + child
    logfh = open(os.path.join(str(out_dir), "run.log"), "ab")
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200,
                             stdin=subprocess.DEVNULL, stdout=logfh, stderr=subprocess.STDOUT)
    else:
        p = subprocess.Popen(cmd, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=logfh, stderr=subprocess.STDOUT)
    logfh.close()
    info = {"pid": p.pid, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": " ".join(cmd), "out": str(out_dir)}
    with open(os.path.join(str(out_dir), "detached.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return (f"LAUNCHED pid={p.pid} out={out_dir} (детач, переживёт этот процесс). "
            f"Агент: чекпоинт -> рестарт (сон). Проснулся: --status -> final_report.md")


def run_analysis(args):
    out_dir = Path(args.out)
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files(args)
    if args.max_files:
        files = files[:args.max_files]
    log(out_dir, f"START files={len(files)} model={args.model} url={args.url} "
                 f"resume={args.resume} detached_child={args.detached_child}")

    prog = load_progress(out_dir) if args.resume else {"done": {}, "failed": {}}
    prog.setdefault("done", {}); prog.setdefault("failed", {})
    prog["total"] = len(files)
    prog["started_at"] = prog.get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    save_progress(out_dir, prog)
    t0 = time.time()
    for i, f in enumerate(files, 1):
        key = str(f)
        if key in prog["done"]:
            continue
        rel = Path(key)
        if args.dir:
            try:
                rel = Path(key).relative_to(args.dir)
            except ValueError:
                rel = Path(key).name
        rpath = reports_dir / (str(rel).replace("\\", "_") + ".md")
        try:
            text = analyze_file(args.url, args.model, args.api_key, f, args.proj, args.max_file_chars)
            rpath.parent.mkdir(parents=True, exist_ok=True)
            with open(rpath, "w", encoding="utf-8") as fh:
                fh.write(f"# Анализ: {key}\n\n{text}\n")
            prog["done"][key] = str(rpath)
            log(out_dir, f"[{i}/{len(files)}] OK {key}")
        except Exception as e:
            prog["failed"][key] = str(e)[:200]
            log(out_dir, f"[{i}/{len(files)}] FAIL {key}: {e}")
        prog["last_file"] = key
        save_progress(out_dir, prog)
        time.sleep(1)  # мягко к LLM

    ok, fail = len(prog["done"]), len(prog["failed"])
    prog["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_progress(out_dir, prog)
    log(out_dir, f"PER-FILE DONE ok={ok} fail={fail} ({time.time()-t0:.0f}s)")
    if ok > 0:
        try:
            fp = final_report(args.url, args.model, args.api_key, str(out_dir), args.proj)
            log(out_dir, f"FINAL REPORT: {fp}")
        except Exception as e:
            log(out_dir, f"FINAL REPORT FAIL: {e}")
    try:
        wk = write_wakeup(out_dir, prog)
        log(out_dir, f"WAKEUP: {wk}")
    except Exception as e:
        log(out_dir, f"WAKEUP FAIL: {e}")
    log(out_dir, "ALL DONE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="корневой каталог (рекурсивно)")
    ap.add_argument("--list", help="файл со списком путей")
    ap.add_argument("--out", required=True, help="каталог результатов")
    ap.add_argument("--url", default=LLM_URL)
    ap.add_argument("--model", default=LLM_MODEL)
    ap.add_argument("--api-key", default=API_KEY)
    ap.add_argument("--ext", nargs="*", default=None, help="фильтр расширений")
    ap.add_argument("--max-files", type=int, default=0, help="лимит файлов (0=все)")
    ap.add_argument("--max-file-chars", type=int, default=12000)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--proj", default="project")
    # v2: sleep-паттерн
    ap.add_argument("--detach", action="store_true",
                    help="запустить процесс в фоне (detached) и немедленно выйти — sleep-паттерн")
    ap.add_argument("--quiet", action="store_true", help="менее словесно (с --detach)")
    ap.add_argument("--status", action="store_true",
                    help="1 строка статуса (total/done/failed/final_report); exit 10 = final готов")
    ap.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.status:
        view, code = status_view(args.out)
        print(json.dumps(view, ensure_ascii=False))
        sys.exit(code)

    if args.detach and not args.detached_child:
        print(spawn_detached(args), flush=True)
        return

    run_analysis(args)




# ============================================================
# bac70ef9 (16.10): v2 SLEEP-ПАТТЕРН. Инструмент-обёртка.
# Режимы: (1) sync — полный прогон; (2) detach — detached-процесс,
# агент: чекпоинт -> рестарт (сон) -> --status -> final_report.md;
# (3) status — 1 строка (final_report готов?).
# Инструкция: tools/BATCH_FILE_ANALYZER_INSTRUKCIYA.md (v2, sleep-паттерн).
# ============================================================
def batch_file_analyzer(dir: str = None, list_file: str = None, out: str = None,
                        url: str = None, model: str = None, api_key: str = None,
                        ext: list = None, max_files: int = 0,
                        max_file_chars: int = 12000, resume: bool = True,
                        proj: str = "project", detach: bool = False,
                        status: bool = False) -> str:
    """Батч-анализ файлов через LLM (v2, sleep-паттерн).
    out ОБЯЗАТЕЛЕН. status=True -> вернуть строку статуса (final_report готов?).
    detach=True -> запустить процесс в фоне (детач) и вернуть сводку; агент затем
    читает status/final_report.md. Иначе — синхронный прогон."""
    if not out:
        raise ValueError("out (каталог результатов) обязателен")
    if status:
        view, code = status_view(out)
        return "STATUS " + __import__("json").dumps(view, ensure_ascii=False)
    if not dir and not list_file and not detach:
        raise ValueError("нужен dir (каталог) или list_file (файл со списком путей)")
    if detach:
        import argparse as _ap
        args = _ap.Namespace(dir=dir, list=list_file, out=out, url=url or LLM_URL,
                             model=model or LLM_MODEL, api_key=api_key or API_KEY,
                             ext=ext, max_files=max_files, max_file_chars=max_file_chars,
                             resume=resume, proj=proj, detach=True, quiet=True,
                             status=False, detached_child=False)
        return spawn_detached(args)
    import argparse as _ap
    args = _ap.Namespace(dir=dir, list=list_file, out=out, url=url or LLM_URL,
                         model=model or LLM_MODEL, api_key=api_key or API_KEY,
                         ext=ext, max_files=max_files, max_file_chars=max_file_chars,
                         resume=resume, proj=proj, detach=False, quiet=False,
                         status=False, detached_child=False)
    log(out, f"START(via-tool) dir={dir} list={list_file} model={args.model}")
    run_analysis(args)
    p = load_progress(out)
    ok, fail = len(p["done"]), len(p["failed"])
    fr = os.path.join(out, "final_report.md")
    fr_note = f", final_report: {fr}" if os.path.exists(fr) else ""
    return f"batch_file_analyzer v2: ok={ok} fail={fail}{fr_note} (out={out}, resumable)"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Прервано. Прогресс сохранён — перезапусти с --resume")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
