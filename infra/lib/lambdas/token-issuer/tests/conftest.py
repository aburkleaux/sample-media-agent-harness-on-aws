"""Make the token-issuer modules importable when pytest runs from the repo.

In Lambda the code asset root is on ``sys.path``, so ``index.py`` imports ``tokens`` directly. Tests
live one level down in ``tests/``, so the asset root is added here.

Each lambda directory has its own top-level modules with no package namespace (gateway-interceptor also
ships an ``index.py``), so a sibling suite collected first would win those names in ``sys.modules``. Evict
modules loaded from a sibling lambda directory so this suite imports its own copies (same approach as
``plan-chainer/conftest.py``). The shared ``_layers`` tree is not evicted.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ASSET = Path(__file__).resolve().parents[1]
_LAMBDAS = _ASSET.parent

_SIBLINGS = [d for d in _LAMBDAS.iterdir() if d.is_dir() and d != _ASSET and d.name != "_layers"]
for _name, _mod in list(sys.modules.items()):
    _file = getattr(_mod, "__file__", None)
    if _file and any(s in Path(_file).resolve().parents for s in _SIBLINGS):
        sys.modules.pop(_name, None)

if str(_ASSET) not in sys.path:
    sys.path.insert(0, str(_ASSET))
