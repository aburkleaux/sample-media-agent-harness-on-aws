"""Print the JSON-spec size of each registered lab tool."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main_lab_tools

total = 0
rows = []
for t in main_lab_tools.LAB_TOOLS:
    spec = t.tool_spec
    s = len(json.dumps(spec))
    total += s
    rows.append((spec["name"], s, spec.get("description", "")[:80]))

rows.sort(key=lambda r: -r[1])
for name, size, desc in rows:
    print(f"  {size:5}  {name:35}  {desc}")
print(f"--- total: {total}")
