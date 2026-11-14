# -*- coding: utf-8 -*-
"""
log_chunk_analyzer.py v2 — bravo c145 (личное задание a13acea9, v1: c142; v2 по V2-бэклогу c144)
Батч-анализ лог-файлов через LLM: файл режется на СРЕЗЫ <= 2500 строк ИЛИ <= 15000 символов
(что наступит раньше), каждый срез -> LLM -> отдельный файл-суммаризация "по какому срезу".
Аналог batch_file_analyzer.py, но с ЧАНКИРОВАНИЕМ внутри файла (batch_file_analyzer усекает
файл до max_file-chars, а здесь весь файл разбирается по срезам).

v2 (c145):
  - LLM: http://127.0.0.1:8080/v1 (gemma; как в batch_file_analyzer; v1 было 127.0.0.1).
  - ANTI-THINKING: в запросе chat_template_kwargs={"enable_thinking": false}
    (Qwen3.x-thinking отдаёт весь ответ в reasoning_content, content пуст — это и было
    «thinking-трейс в суммаризациях»). Если модель всё же дала reasoning-трейс в
    reasoning_content, он ВЫБРАСЫВАЕТСЯ: из него берётся только финальная секция ответа
    (strip_thinking), а не весь многотысячный трейс.
  - остальное (чанкинг, resume, detach, status, final) — как в v1.

ВЫХОД (в --out):
  summaries/<file>_chunk_<i>_of_<n>.md  - суммаризация КАЖДОГО среза (с указанием строк a-b)
  progress.json                        - {files: {name: {lines, chunks_total, chunks_done, status}}}
  final_report.md                      - ИТОГОВЫЙ сводный отчёт (все summaries -> LLM)
  WAKEUP.md                            - будильник для проснувшегося агента
  run.log                              - лог процесса

РЕЖИМЫ:
  python log_chunk_analyzer.py --list list.txt --out <dir>            # синхронно
  python log_chunk_analyzer.py --list list.txt --out <dir> --detach   # detached (sleep-паттерн)
  python log_chunk_analyzer.py --out <dir> --status                   # exit 10 = final готов
  --resume: обработанные срезы пропускаются.
"""
import argparse, json, os, os.path as P, re, subprocess, sys, time, traceback

try:
    import requests
except ImportError:
    sys.exit("Нужен requests: pip install requests")

LLM_URL = "http://127.0.0.1:8080/v1"   # v2: тот же endpoint, что и batch_file_analyzer v2 (gemma 3.55)
API_KEY = "local"
REQUEST_TIMEOUT = 300
MAX_RETRIES = 3
RETRY_BACKOFF = 10
MAX_LINES = 2500        # максимум строк на срез
MAX_CHARS = 15000       # максимум символов на срез
EXIT_FINAL_READY = 10

SYS_PROMPT = (
    "Ты — аналитик логов платёжного Android-терминала (TransCard, Kotlin/Timber). "
    "Тебе дают ОДИН СРЕЗ (кусок) лог-файла — строки с номерами. Формат Timber: 'дата время L [Tag] сообщение', L=I/E/W. "
    "Анализ: (1) что произошло в этом куске — 2-3 строки; (2) ВСЕ ошибки/исключения (уровни E, java.*, 'Failed', 'exception') "
    "с номерами строк и точным текстом; (3) вероятные root-cause и связи между событиями; (4) что важно для дебага "
    "(коды ошибок, HTTP-коды, transactionId, terminalId, версии). СТРОГО только то, что есть в срезе — не выдумывай. "
    "На русском, максимум ~40 строк, без воды."
)
USER_TMPL = """Файл: {path}
Срез {i} из {n} (строки {a}-{b}, всего строк в файле {tot})
Содержимое среза:
```
{content}
```"""

FINAL_TMPL = """Ты — старший аналитик. Ниже — суммаризации ВСЕХ срезов лог-файлов терминала (по одному отчёту на срез).
Сформируй ИТОГОВЫЙ отчёт:
1) Хронология событий (что делал пользователь/терминал);
2) Все ошибки с точными кодами/текстами и их ВЗАИМНАЯ СВЯЗЬ (какая первична, какие следствия);
3) Root-cause: что именно сломано и почему (гипотезы с аргументами из логов);
4) Что нужно сделать (по приоритету P0/P1/P2) — с указанием, кто чинит (сервер/настройки/APK/терминал);
5) Ответы на вопросы: почему операции (наличные/безналичные) не проводились; почему много ПУСТЫХ файлов логов (если видно причину);
6) Что НЕ требует действий.
На русском, структурированно, максимум ~120 строк.

=== СУММАРИЗАЦИИ СРЕЗОВ ===
{reports}"""


def log(out_dir, msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(P.join(str(out_dir), "run.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def strip_thinking(text):
    """v2: если модель всё же вернула reasoning-трейс (reasoning_content при пустом content),
    НЕ тащим весь трейс: берём только финальную секцию ответа.
    Паттерны финала: 'Ответ:'/'Отчёт:'/'Итог:' (последнее вхождение) либо последняя
    секция после '</think>'. Если маркеров нет — хвост <= 4000 символов (трейс длиннее)."""
    t = str(text).strip()
    m = re.search(r"(?:<\|im_end\|>|\n)\s*Ответ\s*:", t)
    if m and m.start() > 200:
        return t[m.start():]
    if "</think>" in t:
        return t.rsplit("</think>", 1)[-1].strip()
    for marker in ("## Отчёт", "## Итог", "ИТОГОВЫЙ ОТЧЁТ", "### Итог"):
        i = t.rfind(marker)
        if i > 200:
            return t[i:]
    return t[-4000:]


def get_model(url):
    try:
        r = requests.get(url + "/models", timeout=15)
        return r.json()["data"][0]["id"]
    except Exception:
        return "Qwen"


def llm_chat(url, model, messages, max_tokens=1500, enable_thinking=False):
    body = {"model": model, "messages": messages,
            "temperature": 0.2, "max_tokens": max_tokens}
    if enable_thinking is False:
        # Qwen3.x (vLLM): выключить thinking — чистый ответ в content, без трейса
        body["chat_template_kwargs"] = {"enable_thinking": False}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url + "/chat/completions",
                              headers={"Authorization": "Bearer " + API_KEY},
                              json=body, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            content = msg.get("content")
            if content is None or not str(content).strip():
                # thinking-модель всё же дала ответ в reasoning* — не тащим весь трейс
                rc = msg.get("reasoning_content") or msg.get("reasoning")
                if rc is None or not str(rc).strip():
                    raise ValueError("empty LLM content")
                content = strip_thinking(rc)
                log(".", "thinking-trace stripped (len %d -> %d)"
                    % (len(str(rc)), len(content)))
            return str(content)
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            log(".", "LLM retry %d after %ds: %s" % (attempt, wait, e))
            time.sleep(wait)


def chunk_file(lines):
    """Режет список строк на срезы <= MAX_LINES и <= MAX_CHARS."""
    chunks = []
    i, n = 0, len(lines)
    while i < n:
        j = min(i + MAX_LINES, n)
        while j > i:
            text = "".join(lines[i:j])
            if len(text) <= MAX_CHARS:
                break
            j -= 1
        if j == i:
            j = i + 1
        chunks.append((i + 1, j, "".join(lines[i:j])))
        i = j
    return chunks


def load_progress(out_dir):
    p = P.join(str(out_dir), "progress.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"files": {}, "final_report": False, "finished_at": None}


def save_progress(out_dir, prog):
    tmp = P.join(str(out_dir), "progress.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=1)
    os.replace(tmp, P.join(str(out_dir), "progress.json"))


def analyze_one_file(url, model, fpath, out_dir, prog):
    name = P.basename(str(fpath))
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        prog["files"][name] = {"status": "error", "error": str(e)}
        save_progress(out_dir, prog)
        return
    if not lines:
        prog["files"][name] = {"status": "skipped_empty", "lines": 0}
        save_progress(out_dir, prog)
        return
    chunks = chunk_file(lines)
    st = prog["files"].get(name, {})
    st.update({"lines": len(lines), "chunks_total": len(chunks), "chunks_done": st.get("chunks_done", 0), "status": "working"})
    prog["files"][name] = st
    sdir = P.join(str(out_dir), "summaries")
    os.makedirs(sdir, exist_ok=True)
    for idx, (a, b, text) in enumerate(chunks, 1):
        sfile = P.join(sdir, "%s_chunk_%d_of_%d.md" % (name, idx, len(chunks)))
        if os.path.exists(sfile) and os.path.getsize(sfile) > 50:
            continue  # уже обработан (resume)
        user = USER_TMPL.format(path=str(fpath), i=idx, n=len(chunks), a=a, b=b,
                                tot=len(lines), content=text)
        try:
            ans = llm_chat(url, model, [{"role": "system", "content": SYS_PROMPT},
                                        {"role": "user", "content": user}])
            header = "Суммаризация среза %d/%d файла %s (строки %d-%d из %d)\n\n" % (idx, len(chunks), name, a, b, len(lines))
            with open(sfile, "w", encoding="utf-8") as f:
                f.write(header + ans + "\n")
            st["chunks_done"] = idx
            log(out_dir, "  %s chunk %d/%d done" % (name, idx, len(chunks)))
        except Exception as e:
            st["status"] = "failed_chunk_%d" % idx
            log(out_dir, "  %s chunk %d FAILED: %s" % (name, idx, e))
            break
        save_progress(out_dir, prog)
    if st["chunks_done"] >= len(chunks):
        st["status"] = "done"
    save_progress(out_dir, prog)


def build_final(url, model, out_dir, prog):
    sdir = P.join(str(out_dir), "summaries")
    entries = []
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if fn.endswith(".md"):
                p = P.join(sdir, fn)
                entries.append((fn, open(p, encoding="utf-8", errors="ignore").read()))
    if not entries:
        return
    parts, cur, cur_len = [], [], 0
    for name, txt in entries:
        if cur_len + len(txt) + 200 > 24000 and cur:
            parts.append("\n\n".join(cur)); cur, cur_len = [], 0
        cur.append("### %s\n%s" % (name, txt[:2200])); cur_len += len(txt)
    if cur:
        parts.append("\n\n".join(cur))
    final = []
    for i, p in enumerate(parts):
        n = len(entries)
        final.append(llm_chat(url, model,
            [{"role": "system", "content": SYS_PROMPT},
             {"role": "user", "content": FINAL_TMPL.format(n=n, reports=p)}], max_tokens=8000))
    with open(P.join(str(out_dir), "final_report.md"), "w", encoding="utf-8") as f:
        f.write("# ИТОГОВЫЙ ОТЧЁТ — log_chunk_analyzer v2 (bravo c145)\n\n" + "\n\n---\n\n".join(final) + "\n")
    prog["final_report"] = True
    prog["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_progress(out_dir, prog)
    wake = ("WAKEUP (log_chunk_analyzer v2): final_report.md ГОТОВ (%s). "
            "Читай: %s — и докладывай (HR/отчёт). Суммаризации срезов: %s/summaries/\n") % (
        prog["finished_at"], P.join(str(out_dir), "final_report.md"), out_dir)
    with open(P.join(str(out_dir), "WAKEUP.md"), "w", encoding="utf-8") as f:
        f.write(wake)
    log(out_dir, "FINAL_REPORT ready")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--list")
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default=LLM_URL)
    ap.add_argument("--api-key", default=API_KEY)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--detach", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out = args.out
    os.makedirs(P.join(out, "summaries"), exist_ok=True)

    if args.status:
        prog = load_progress(out)
        ready = prog.get("final_report", False)
        tail = ""
        rl = P.join(out, "run.log")
        if os.path.exists(rl):
            with open(rl, encoding="utf-8", errors="ignore") as f:
                tail = "".join(f.readlines()[-5:])
        print(json.dumps({"total_files": len(prog.get("files", {})),
                          "files": prog.get("files", {}),
                          "final_report": ready, "finished_at": prog.get("finished_at"),
                          "tail": tail}, ensure_ascii=False))
        sys.exit(EXIT_FINAL_READY if ready else 0)

    if args.detach:
        cmd = [sys.executable, os.path.abspath(__file__), "--out", out]
        if args.list:
            cmd += ["--list", args.list]
        if args.dir:
            cmd += ["--dir", args.dir]
        if args.url:
            cmd += ["--url", args.url]
        if args.resume:
            cmd += ["--resume"]
        lf = open(P.join(out, "run.log"), "a", encoding="utf-8")
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                             cwd=P.dirname(os.path.abspath(__file__)),
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(P.join(out, "detached.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": p.pid, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cmd": " ".join(cmd)}, f)
        print("LAUNCHED pid=%d" % p.pid)
        sys.exit(0)

    # files list
    files = []
    if args.list:
        with open(args.list, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    files.append(ln)
    elif args.dir:
        for dirpath, _, filenames in os.walk(args.dir):
            for fn in sorted(filenames):
                files.append(P.join(dirpath, fn))
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        log(out, "NO FILES")
        sys.exit(1)

    model = get_model(args.url)
    log(out, "start: %d files, model=%s" % (len(files), model))
    prog = load_progress(out)
    for f in files:
        name = P.basename(f)
        st = prog["files"].get(name, {})
        if st.get("status") == "done" and args.resume:
            continue
        log(out, "file: %s" % name)
        analyze_one_file(args.url, model, f, out, prog)
    # final
    all_done = all(p.get("status") == "done" for p in prog["files"].values()) and prog["files"]
    if all_done:
        if prog.get("final_report") and args.resume:
            log(out, "final already done (resume)")
        else:
            try:
                build_final(args.url, model, out, prog)
            except Exception as e:
                log(out, "FINAL FAILED: %s\n%s" % (e, traceback.format_exc()))
    else:
        log(out, "not all done -> final later")
    log(out, "done. final_report=%s" % prog.get("final_report"))


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# АГЕНТНЫЙ ВХОД (registry: tools.log_chunk_analyzer.log_chunk_analyzer).
# main() — чистый CLI (argparse, sys.exit). Для вызова инструментом агентом нужен
# отдельный entry: обёртка через subprocess, чтобы sys.exit/argv не мешали и
# чтобы вызов был idempotent для tool-loop.
# ---------------------------------------------------------------------------
def log_chunk_analyzer(list_file=None, dir=None, out=None, url=None,
                       resume=False, detach=False, status=False):
    """Батч-анализ лог-файлов по срезам через LLM (вход для агента-инструмента).
    out — ОБЯЗАТЕЛЬНЫЙ каталог результатов.
      status=True  -> вернуть JSON-строку статуса (final_report готов?).
      detach=True  -> запуск в фоне (детач) -> сводка LAUNCHED pid=...;
                      агент: чекпоинт -> рестарт; затем --status / final_report.md.
      иначе        -> синхронный прогон -> сводка done/fail/final_report.
    list_file / dir — источник файлов (нужен хотя бы один для запуска).
    url — LLM-сервер; по умолчанию — СВОЙ LLM-сервер агента (llama_server_url
           из runtime-настроев, без хвоста /v1).
    """
    if not out:
        raise ValueError("out (каталог результатов) обязателен")
    import subprocess as _sp
    import os.path as _P

    # URL: собственный LLM-сервер агента (без /v1), т.к. модуль делает
    # url+'/models' и url+'/chat/completions'.
    _u = url
    if not _u:
        try:
            import importlib as _im
            _pkg_root = _P.dirname(_P.dirname(_P.dirname(_P.abspath(__file__))))
            if _pkg_root not in sys.path:
                sys.path.insert(0, _pkg_root)
            _pkg_name = _P.basename(_P.dirname(_P.dirname(_P.abspath(__file__))))
            _cfgmod = _im.import_module(_pkg_name + ".core.config")
            _u = _cfgmod.get_runtime_settings().get("llama_server_url", "")
        except Exception:
            _u = ""
        if not _u:
            _u = LLM_URL
    _u = _u.rstrip('/')
    if _u.endswith('/v1'):
        _u = _u[:-3]
    _u = _u.rstrip('/')

    cmd = [sys.executable, os.path.abspath(__file__), "--out", out, "--url", _u]
    if status:
        cmd += ["--status"]
    elif detach:
        cmd += ["--detach"]
    elif list_file is not None:
        cmd += ["--list", list_file]
    elif dir is not None:
        cmd += ["--dir", dir]
    else:
        if not (status or detach):
            raise ValueError("нужен list_file (или dir) для запуска; либо статус=status/detach")
    if resume:
        cmd += ["--resume"]

    p = _sp.run(cmd, capture_output=True, text=True,
                cwd=_P.dirname(os.path.abspath(__file__)))
    out_str = (p.stdout or "").strip()
    if not out_str and p.stderr:
        out_str = (p.stderr or "").strip()

    if status:
        return ("log_chunk_analyzer STATUS exit=%d:\n%s"
                % (p.returncode, (out_str or "(empty)")[-3000:]))
    if detach:
        return "log_chunk_analyzer DETACHED: " + (out_str or "(see detached.json in " + out + ")")

    # sync: собрать сводку из progress.json (файл пишется самим main())
    try:
        prog = load_progress(out)
        files = prog.get("files", {})
        done = sum(1 for s in files.values() if s.get("status") == "done")
        failed = sum(1 for s in files.values() if str(s.get("status", "")).startswith("failed"))
        skipped = sum(1 for s in files.values() if s.get("status") == "skipped_empty")
        fr = os.path.join(out, "final_report.md")
        fr_note = (", final_report: " + fr) if os.path.exists(fr) else ""
        return ("log_chunk_analyzer: files=%d done=%d failed=%d skipped=%d%s (out=%s)"
                % (len(files), done, failed, skipped, fr_note, out))
    except Exception as e:
        return "log_chunk_analyzer sync done (progress.json read error): %s" % (str(e)[:300])
