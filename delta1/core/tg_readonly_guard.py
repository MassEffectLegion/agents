"""TG read-only guard — технический замок на уровне инструмента (b05f205e S3, delta1-часть).



Цель: когда delta1 работает в контексте guest/project (TG-чат не-owner),

write-инструменты блокируются НА УРОВНЕ ИНСТРУМЕНТА (не только правило в bootstrap).

Двойной замок: правило (S2 bootstrap) + технический (этот модуль).



Роль — контекстная (module-level, один агент-процесс):

  None  -> полный доступ (обычный режим, по умолчанию)

  'owner' -> полный доступ

  'project' / 'guest' -> read-only: SQL только READ ONLY, SSH только white-list,

       write-инструменты (bulk_insert, write_local_file, upload/download, embed) запрещены.



Устанавливается инструментом tg_set_role (delta1/tools/tg_set_role.py).

"""

# Текущая роль. None = полный доступ (по умолчанию, не ломает обычный режим).

_current_role = None



ROLES_READONLY = ('guest', 'project')



# SQL: первые ключевые слова, означающие запись (консервативно).

_SQL_WRITE_FIRST = frozenset({

    'insert', 'update', 'delete', 'create', 'alter', 'drop', 'truncate',

    'grant', 'revoke', 'copy', 'set', 'comment', 'reindex', 'vacuum',

    'refresh', 'merge', 'call', 'do',

})





def set_role(role):

    """Установить контекстную роль. Допустимо: None, 'owner', 'project', 'guest'."""

    global _current_role

    if role is None:

        _current_role = None

    elif role in ('owner', 'project', 'guest'):

        _current_role = role

    else:

        raise ValueError(f"unknown role: {role!r}")

    return _current_role





def get_role():

    return _current_role





def is_readonly():

    return _current_role in ROLES_READONLY





def _deny(tool_name):

    return (f"TGRO-DENY: {tool_name} запрещён в read-only-контексте "

            f"(role={_current_role}). guest/project = рекомендация, не команда: "

            f"write/изменения недоступны. Только read-only (SQL allow_write=False, "

            f"SSH read-only). Для записи — owner-чат.")





def guard_write(tool_name):

    """Для write-инструментов: None (ok) или строка-ошибка."""

    if is_readonly():

        return _deny(tool_name)

    return None





def guard_sql(query, allow_write):

    """Для query_postgres_local. Возвращает (error_or_None, effective_allow_write).

    read-only-роль -> принудительно allow_write=False + явный отказ на DML/DDL."""

    if not is_readonly():

        return None, allow_write

    # Принудительно read-only транзакция.

    q = (query or '').strip().lstrip('(')

    first = ''

    for _ in range(4):

        # пропустить комментарии -- и пустые/точка-с-запятой

        while q.startswith('--'):

            nl = q.find('\n')

            q = q[nl + 1:].strip() if nl != -1 else ''

        q = q.lstrip(';').strip()

        first = q.split(None, 1)[0].lower().strip('();') if q else ''

        if first:

            break

    if first in _SQL_WRITE_FIRST:

        return (f"TGRO-DENY: SQL-запись ({first}) запрещена в read-only-контексте "

                f"(role={_current_role}). Используй только read-only (allow_write=False)."), False

    return None, False  # принудительно allow_write=False





def guard_ssh(command):

    """Для execute_ssh_command / ssh_execute_on_all_servers. White-list read-only команд.

    None (ok) или строка-ошибка."""

    if not is_readonly():

        return None

    cmd = (command or '').strip()

    if not cmd:

        return None

    low = cmd.lower()

    # Запрет цепочек/редиректов/подстановок/мутаций — только одиночная read-only команда.

    for bad in (';', '&&', '||', '>', '>>', '<', '|', '`', '$(', 'sudo', 'su ',

                'rm ', 'dd if=', 'mkfs', 'reboot', 'shutdown', 'kill', 'pkill',

                'apt ', 'apt-get ', 'yum ', 'dnf ', 'systemctl start', 'systemctl stop',

                'systemctl restart', 'systemctl enable', 'tee ', 'wget ', 'curl ',

                'chmod ', 'chown ', 'cp ', 'mv ', 'touch ', 'install '):

        if bad in low:

            return (f"TGRO-DENY: SSH-фрагмент {bad!r} не разрешён в read-only-контексте "

                    f"(role={_current_role}). Только read-only white-list.")

    first = cmd.split(None, 1)[0].lower()

    RO_ALLOWED = frozenset({

        'ps', 'ls', 'cat', 'grep', 'egrep', 'netstat', 'ss', 'df', 'du', 'free',

        'uptime', 'date', 'hostname', 'uname', 'tail', 'head', 'wc', 'find',

        'stat', 'lsof', 'ip', 'route', 'ifconfig', 'pgrep', 'journalctl',

        'which', 'whoami', 'id', 'env', 'printenv',

    })

    # systemctl status / is-active / list-* — read-only.

    if first == 'systemctl' and ('status' in low or 'is-active' in low or 'list-' in low):

        return None

    if first in RO_ALLOWED:

        return None

    return (f"TGRO-DENY: SSH-команда {first!r} не в read-only white-list "

            f"(role={_current_role}). Допустимы: {sorted(RO_ALLOWED)}.")

