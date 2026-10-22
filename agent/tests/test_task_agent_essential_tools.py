"""Fix 2 — the Task Agent guarantees its core builtins on an INTERNAL dispatch.

Prod session 738cd97b: the orchestrator dispatched a PDF/code task forwarding only
`artifact`, so the sub-agent had NO `code_interpreter` and could not build the file
("no code_interpreter tool in my function list"). The Task Agent is the harness's
code+artifact sub-agent, so on an internal dispatch that named a specific tool list
those two builtins are always made available. The external (principal-bearing) path
must stay narrowed (confused-deputy guard) and the []/None cases are left as-is.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import task_agent as ta  # noqa: E402


ART = object()   # stand-in for the artifact tool
CI = object()    # stand-in for the code_interpreter tool
GW = object()    # stand-in for a forwarded gateway tool


def _patch(monkeypatch):
    monkeypatch.setattr(
        ta, "_BUILTINS",
        {"artifact": ART, "code_interpreter": CI, "file_read": object()},
    )


def test_internal_specific_list_gets_both_core_builtins(monkeypatch):
    _patch(monkeypatch)
    out = ta._ensure_essential_builtins([GW], ["some_gw"], principal=None)
    assert GW in out and ART in out and CI in out


def test_internal_artifact_only_gains_code_interpreter(monkeypatch):
    _patch(monkeypatch)
    out = ta._ensure_essential_builtins([ART], ["artifact"], principal=None)
    assert ART in out and CI in out
    assert out.count(ART) == 1  # no duplicate of the already-present tool


def test_external_principal_path_is_not_widened(monkeypatch):
    _patch(monkeypatch)
    # Confused-deputy guard: external opt-in narrowing must NOT be widened.
    out = ta._ensure_essential_builtins([ART], ["artifact"], principal={"issuer": "i", "sub": "s"})
    assert out == [ART]  # code_interpreter NOT added on the external path


def test_empty_list_is_pure_llm_untouched(monkeypatch):
    _patch(monkeypatch)
    assert ta._ensure_essential_builtins([], [], principal=None) == []


def test_none_requested_is_untouched(monkeypatch):
    _patch(monkeypatch)
    # None is handled by the full-catalog branch in invoke (already has both);
    # the helper guards it as a no-op.
    assert ta._ensure_essential_builtins([GW], None, principal=None) == [GW]


def test_no_duplicate_when_both_already_present(monkeypatch):
    _patch(monkeypatch)
    out = ta._ensure_essential_builtins(
        [ART, CI, GW], ["artifact", "code_interpreter", "some_gw"], principal=None
    )
    assert out.count(CI) == 1 and out.count(ART) == 1 and GW in out
