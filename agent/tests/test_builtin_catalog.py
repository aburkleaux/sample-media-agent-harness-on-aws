"""The builtin tool projection is published so the catalog need not wake the runtime.

Loading the tool catalogue cost a full runtime invocation — measured 8.6s cold
against 0.2s on an already-warm session, because the proxy sends no session id
and therefore cold-starts every request. Gateway tools can be read straight from
the AgentCore control plane, but builtin specs exist ONLY inside the runtime
process, so without publishing them the reader stays on the slow path and the
control-plane work buys nothing.

Written best-effort: a cache write must never stop the agent from starting.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

MAIN = (SRC / "main.py").read_text()


class _Table:
    """Captures put_item calls, or raises to exercise the failure path."""

    def __init__(self, boom=False):
        self.items = []
        self.boom = boom

    def put_item(self, Item=None, **kw):
        if self.boom:
            raise RuntimeError("throughput exceeded")
        self.items.append(Item)
        return {}


class TestPublishBuiltinCatalog:
    def _mod(self, monkeypatch, table, tools):
        import main
        monkeypatch.setitem(main.__dict__, "_catalog_table", table)
        monkeypatch.setattr(main, "_get_builtin_tools", lambda: tools)
        return main

    def test_writes_the_projection_under_a_stable_key(self, monkeypatch):
        t = _Table()
        m = self._mod(monkeypatch, t, [{"name": "artifact", "description": "d", "state": "trusted"}])
        assert m._publish_builtin_catalog() is True
        item = t.items[0]
        assert item["PK"] == m._CATALOG_PK == "GATEWAY#CATALOG"
        assert item["SK"] == m._CATALOG_BUILTIN_SK == "__builtin__"

    def test_carries_every_tool_and_a_count(self, monkeypatch):
        tools = [{"name": f"t{i}", "description": "", "state": "available"} for i in range(45)]
        t = _Table()
        m = self._mod(monkeypatch, t, tools)
        m._publish_builtin_catalog()
        assert t.items[0]["tool_count"] == 45
        assert len(t.items[0]["tools"]) == 45

    def test_records_when_it_was_published(self, monkeypatch):
        """A reader needs to reason about staleness."""
        t = _Table()
        m = self._mod(monkeypatch, t, [])
        m._publish_builtin_catalog()
        assert t.items[0]["published_at"].startswith("20")

    def test_sits_beside_the_gateway_descriptors(self, monkeypatch):
        """Same table as GATEWAY#REGISTERED — no new table introduced."""
        t = _Table()
        m = self._mod(monkeypatch, t, [])
        m._publish_builtin_catalog()
        assert t.items[0]["PK"].startswith("GATEWAY#")

    def test_a_write_failure_never_raises(self, monkeypatch):
        """Startup must not fail because a cache write did."""
        m = self._mod(monkeypatch, _Table(boom=True), [{"name": "x"}])
        assert m._publish_builtin_catalog() is False

    def test_no_table_configured_is_a_clean_skip(self, monkeypatch):
        import main
        monkeypatch.setitem(main.__dict__, "_catalog_table", None)
        assert main._publish_builtin_catalog() is False


class TestWiring:
    """A projection that is never written, or goes stale, is worse than none."""

    def test_published_at_startup(self):
        assert "_publish_builtin_catalog()" in MAIN
        # After the gateway load, so the recorded state is the final tool set.
        gw = MAIN.index("Gateway registry unavailable; starting with zero gateways")
        assert MAIN.index("_publish_builtin_catalog()", gw) > gw

    def test_republished_when_a_builtin_is_toggled(self):
        """`state` is part of the projection, so add/remove must refresh it."""
        add = MAIN.index("_added_builtins.add(n)")
        rem = MAIN.index("_added_builtins.discard(n)")
        assert "_publish_builtin_catalog()" in MAIN[add:add + 220]
        assert "_publish_builtin_catalog()" in MAIN[rem:rem + 260]

    def test_table_handle_is_bound_during_construction(self):
        assert '_catalog_table' in MAIN
        assert 'globals()["_catalog_table"]' in MAIN

    def test_timezone_is_imported(self):
        """datetime.now(timezone.utc) NameErrors if only datetime is imported."""
        assert "from datetime import datetime, timezone" in MAIN
