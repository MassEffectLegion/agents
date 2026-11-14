"""Rebuild vendor from verified local wheels using only bundled Python."""
import runpy
import sys
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path.insert(0,str(root))
from recovery import manifest_check
if sys.version_info[:2]!=(3,11) or sys.platform!='win32':
    raise SystemExit('This offline wheel set is built for Windows x64 / Python 3.11.')
checks=manifest_check(root/'offline/wheels')
if not checks or not all(checks.values()):
    raise SystemExit('Offline wheel integrity check failed')
pip_wheels=list((root/'offline/wheels').glob('pip-*.whl'))
if len(pip_wheels)!=1:
    raise SystemExit('Exactly one bundled pip wheel is required')
sys.path.insert(0,str(pip_wheels[0]))
sys.argv=['pip','install','--no-index','--find-links',str(root/'offline/wheels'),
          '--target',str(root/'vendor'),'--upgrade','--ignore-installed','-r',str(root/'requirements-lock.txt')]
runpy.run_module('pip',run_name='__main__',alter_sys=True)
