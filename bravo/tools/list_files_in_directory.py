"""List files and directories with pagination and canonical path checks."""
import os
from bravo.tools.path_guard import require_allowed, is_allowed

def list_files_in_directory(directory_path: str, recursive: bool = False,
                            sort_by: str = 'name', offset: int = 0, limit: int = 100) -> str:
    """List files AND directories. Use offset/limit to see later entries; recursive traverses subdirectories."""
    try:
        base = require_allowed(directory_path)
        entries = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if is_allowed(os.path.join(root, d))]
            for name in dirs + files:
                path = os.path.join(root, name)
                if not is_allowed(path):
                    continue
                directory = os.path.isdir(path)
                entries.append((os.path.relpath(path, base), 'dir' if directory else 'file', 0 if directory else os.path.getsize(path)))
            if not recursive:
                break
        entries.sort(key=(lambda x: -x[2]) if sort_by == 'size' else (lambda x: x[0].lower()))
        offset, limit = max(0, offset), max(1, min(limit, 200))
        page = entries[offset:offset+limit]
        return f'{base}: {len(entries)} entries, {sum(e[2] for e in entries)} file bytes; offset={offset}\n' + '\n'.join(
            f'{kind} {size:>10} {name}' for name, kind, size in page) + (
            f'\nNext offset: {offset+limit}' if offset+limit < len(entries) else '')
    except Exception as exc:
        return f'ERROR: {exc}'
