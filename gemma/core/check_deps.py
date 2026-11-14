"""Check time import and cycle_settings scope in both main.py files."""
import io, re
for name, p in [("bravo", r"<AGENTS_DIR>\workdir\bravo\main.py"),
                ("alfa", r"<AGENTS_DIR>\workdir\alfa\main.py")]:
    txt = io.open(p, encoding="utf-8").read()
    lines = txt.splitlines()
    print("=== %s ===" % name)
    for i, l in enumerate(lines):
        s = l.strip()
        if re.match(r"^import ", s) or re.match(r"^from ", s):
            print("  L%d: %s" % (i+1, s[:110]))
    print("  time.sleep count:", txt.count("time.sleep"))
    for i, l in enumerate(lines):
        if "cycle_settings" in l and ("=" in l) and ("def " not in l):
            print("  cycle_settings at L%d: %s" % (i+1, l.strip()[:100]))
    # def cycle signature
    for i, l in enumerate(lines):
        if l.strip().startswith("def ") and ("cycle" in l.lower() or "run" in l.lower()):
            print("  def L%d: %s" % (i+1, l.strip()[:100]))
