"""Make the shared result-resolution layer importable during tests.

In Lambda the layer is unpacked to ``/opt/python``, which is already on
``sys.path`` -- so ``handler.py`` can ``import result_resolution`` directly. Tests
run from the repo, where that mount does not exist, so this reproduces it.

Deliberately NOT solved by wrapping the import in try/except inside handler.py: a
missing layer in production would then degrade silently, and aggregation failing
quietly is the class of bug this whole change exists to remove.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LAYER = Path(__file__).resolve().parents[1] / "_layers" / "result-resolution" / "python"
if str(_LAYER) not in sys.path:
    sys.path.insert(0, str(_LAYER))

# --- sibling-lambda module shadowing -------------------------------------------------------
#
# Each lambda directory has its OWN top-level modules (``handler.py``, ``gateway_invoker.py``,
# ...) with no package namespace, so the FIRST directory imported wins those names in
# ``sys.modules`` for the whole pytest session. Running the lambda suites TOGETHER therefore
# pointed one directory's tests at a SIBLING's module:
#   AttributeError: <module 'handler' from '.../plan-chainer/handler.py'>
#       has no attribute '_invoke_status'
# Ten tests failed that way, and — worse than failing — any name the two modules happen to
# share was silently exercised against the wrong implementation.
#
# A conftest is imported before its own directory's tests are collected, so evicting modules
# that were loaded from a sibling LAMBDA directory makes this directory import its own copies.
# Sibling test modules already collected keep the object they bound at import time, so they are
# unaffected. The shared ``_layers`` tree is deliberately NOT evicted: it is one implementation
# shared by every lambda, which is the point of a layer.
import sys as _sys
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
_SIBLING_LAMBDAS = [
    _d for _d in _HERE.parent.iterdir()
    if _d.is_dir() and _d != _HERE and (_d / "handler.py").exists()
]
for _name, _mod in list(_sys.modules.items()):
    _file = getattr(_mod, "__file__", None)
    if not _file:
        continue
    _resolved = _Path(_file).resolve()
    if any(_sib in _resolved.parents for _sib in _SIBLING_LAMBDAS):
        _sys.modules.pop(_name, None)
