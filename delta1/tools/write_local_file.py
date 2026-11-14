"""
write_local_file — запись текстового файла (создание/перезапись).

P1-1 (rev 1.2, 2026-09-22): prefix-баг путей — сравнение с разделителем
(<AGENTS_DIR>\workdir_evil больше не проходит как <AGENTS_DIR>\workdir).
P2-14: os.makedirs не падает при пустом dirname (relative-имя).
"Инструменты на свой каталог своего агента" (человек, b3388012):
ALLOWED_DIRS — из СВОЕГО core/config.py, а не локальная копия.
"""
import os

from delta1.core.config import ALLOWED_DIRS


from delta1.tools.path_guard import is_allowed as _is_allowed, resolve_path
from delta1.core import tg_readonly_guard as _tgro


def write_local_file(file_path: str, content: str) -> str:
    """Create or overwrite a local text file. Auto-creates parent directories.
    Args:
        file_path: Target file path (must be in allowed directories)
        content: Text to write
    """
    _g = _tgro.guard_write('write_local_file')
    if _g:
        return _g
    try:
        abs_path = resolve_path(file_path)
        if not _is_allowed(abs_path):
            return f"ERROR: Access denied. Allowed dirs: {ALLOWED_DIRS}"

        parent = os.path.dirname(abs_path)
        if parent:  # P2-14: dirname может быть '' для relative-имени
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"✓ saved: {abs_path}"
    except Exception as e:
        return f"ERROR: {str(e)}"
