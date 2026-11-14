"""

sql_safety.py — общие хелперы безопасности SQL-инструментов (delta1).



ПАТЧ-2 H2/H3 (2026-09-22, OK человека b3388012):

- _safe_ident: валидация SQL-идентификатора (имя таблицы/колонки) перед

  подстановкой в SQL f-строкой. Защита от инъекции через параметр инструмента.

  (БД локальная, доступа нет ни у кого кроме человека и агентов, но

  дисциплина: идентификаторы валидируются всегда.)

- _strip_strings_and_comments / _classify: классификация запроса по первому

  ключевому слову (используется query_postgres_local, H1).

"""

import re



_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")





def _safe_ident(name: str) -> str:

    """Валидирует SQL-идентификатор (table_name / column_name).

    Разрешены: identifier и schema.identifier. Иначе — ValueError."""

    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name.strip()):

        raise ValueError(f"Недопустимый SQL-идентификатор: {name!r}")

    return name.strip()





def _strip_strings_and_comments(q: str) -> str:

    """Снимает SQL-строковые литералы и комментарии."""

    q = re.sub(r"'(?:[^']|'')*'", "''", q)      # 'строки' -> ''

    q = re.sub(r'"(?:[^"]|"")*"', '""', q)      # "идентификаторы" -> ""

    q = re.sub(r"--[^\n]*", " ", q)              # -- комментарии

    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S) # /* */ комментарии

    return q





def _classify(q: str):

    """Первое ключевое слово запроса (WITH/SELECT/INSERT/UPDATE/CREATE/DROP/

    TRUNCATE/DELETE/ALTER) или None (неопознанно -> отклонить)."""

    s = _strip_strings_and_comments(q).strip().lstrip(';').strip().upper()

    m = re.match(r"(WITH|SELECT|INSERT|UPDATE|CREATE|DROP|TRUNCATE|DELETE|ALTER)\b", s)

    return m.group(1) if m else None



def table_parts(name):

    name = _safe_ident(name)

    parts = name.split('.')

    return tuple(parts) if len(parts) == 2 else ('public', parts[0])

