from omega.core.config import get_runtime_settings
"""Bounded file reads with a continuation offset for very long lines."""
from omega.tools.path_guard import require_allowed

def read_local_file_chunked(file_path: str, start_line: int = 1, end_line: int = None,
                            max_chars: int = 8000, start_char: int = 0) -> str:
    """Read text in bounded chunks. If a line is split, resume at next_line/next_char.
    start_char is a zero-based character offset within start_line; line numbers are one-based.
    """
    try:
        path = require_allowed(file_path)
        if start_line < 1 or start_char < 0 or (end_line is not None and end_line < start_line):
            return 'ERROR: Invalid line range or character offset'
        limit = max(1, min(max_chars, get_runtime_settings()['file_read_max_chars']))
        parts, used = [], 0
        last_line = start_line
        with open(path, encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                if number < start_line:
                    continue
                if end_line is not None and number > end_line:
                    break
                offset = start_char if number == start_line else 0
                available = line[offset:]
                piece = available[:limit-used]
                parts.append(piece)
                used += len(piece)
                last_line = number
                if len(piece) < len(available):
                    return f'{path}: lines {start_line}-{number}; next_line={number}, next_char={offset+len(piece)}\n' + ''.join(parts)
                if used >= limit:
                    return f'{path}: lines {start_line}-{number}; next_line={number+1}, next_char=0\n' + ''.join(parts)
        return f'{path}: lines {start_line}-{last_line}; end of range/file\n' + ''.join(parts)
    except Exception as exc:
        return f'ERROR: {exc}'
