"""Canonical, agent-local path checks (including junction/symlink resolution)."""
import os
from gemma.core.config import AGENT_ROOT, ALLOWED_DIRS

def resolve_path(path):
    return os.path.realpath(os.path.join(str(AGENT_ROOT), os.path.expanduser(str(path))))

def is_allowed(path, allowed_dirs=None):
    target = os.path.normcase(resolve_path(path))
    for root in ALLOWED_DIRS if allowed_dirs is None else allowed_dirs:
        root = os.path.normcase(os.path.realpath(root))
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except ValueError:
            continue
    return False

def require_allowed(path):
    target = resolve_path(path)
    if not is_allowed(target):
        raise ValueError(f'Access denied: {path}')
    return target
