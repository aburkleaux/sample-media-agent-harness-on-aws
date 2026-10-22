"""Quick syntax check for the lab module after refactor sweeps."""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "src")
fail = 0
for dirpath, _dirs, files in os.walk(ROOT):
    if "__pycache__" in dirpath:
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        full = os.path.join(dirpath, fn)
        try:
            ast.parse(open(full).read())
        except SyntaxError as e:
            print(f"FAIL {full}: {e}")
            fail += 1
print(f"--- {fail} failures ---")
sys.exit(0 if fail == 0 else 1)
