"""Portable launcher: local vendor packages, no activated venv or global packages."""
import os
import runpy
import sys
from pathlib import Path
root=Path(__file__).resolve().parent
vendor=root/'vendor'
sys.path.insert(0,str(vendor))
sys.path.insert(0,str(root.parent))
if len(sys.argv)>2 and sys.argv[1]=='-m':
    del sys.argv[1]
    module=sys.argv.pop(1)
    sys.argv[0]=module
    runpy.run_module(module,run_name='__main__',alter_sys=True)
    raise SystemExit(0)
if len(sys.argv)<2:
    script=root/'main.py'
else:
    script=Path(sys.argv.pop(1))
    if not script.is_absolute():
        script=root/script
sys.argv[0]=str(script)
runpy.run_path(str(script),run_name='__main__')
