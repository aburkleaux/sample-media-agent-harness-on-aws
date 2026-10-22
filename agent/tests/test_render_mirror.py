"""Mirroring external media into our bucket before rendering."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from hooks import actions

FAL = "https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45Lp.jpg"
CF = "dexamplecdn002.cloudfront.net"
OURS = f"https://{CF}/artifacts/external/abc123.jpg"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CF)
    monkeypatch.setenv("S3_PREFIX", "artifacts/")
    actions._S3_CLIENT = object()          # non-None so no boto3 client is built
    actions._source_urls_seen.clear()
    yield
    actions._S3_CLIENT = None
    actions._source_urls_seen.clear()


class TestMirror:
    def test_external_url_is_replaced_with_our_copy(self, monkeypatch):
        from media import persist_media
        monkeypatch.setattr(persist_media, "persist_url",
                            lambda url, **kw: {"url": OURS})
        assert actions._mirror_external_media([FAL]) == [OURS]

    def test_our_own_urls_are_left_alone(self):
        """No copy, no fetch -- re-mirroring our own asset would duplicate it on
        every turn that mentions it."""
        assert actions._mirror_external_media([OURS]) == [OURS]

    def test_non_media_untouched(self):
        assert actions._mirror_external_media(["https://h/page.html"]) == \
            ["https://h/page.html"]

    def test_order_and_mixture_preserved(self, monkeypatch):
        from media import persist_media
        monkeypatch.setattr(persist_media, "persist_url", lambda url, **kw: {"url": OURS})
        got = actions._mirror_external_media([OURS, FAL, "https://h/p.html"])
        assert got == [OURS, OURS, "https://h/p.html"]

    def test_a_copy_failure_passes_the_source_through(self, monkeypatch):
        """Worst case must be the OLD behaviour -- a link -- not a lost render."""
        from media import persist_media
        def boom(url, **kw):
            raise persist_media.PersistError("host refused")
        monkeypatch.setattr(persist_media, "persist_url", boom)
        assert actions._mirror_external_media([FAL]) == [FAL]

    def test_no_bucket_configured_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        assert actions._mirror_external_media([FAL]) == [FAL]

    def test_source_urls_are_tracked_for_dedupe(self, monkeypatch):
        """Both the original and our copy must count as dispatched, or the next
        tool result quoting the third-party url mirrors and renders again."""
        from media import persist_media
        monkeypatch.setattr(persist_media, "persist_url", lambda url, **kw: {"url": OURS})
        actions._mirror_external_media([FAL])
        assert FAL in actions._source_urls_seen
