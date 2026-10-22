"""Copying external media into our bucket."""
import sys, os, hashlib, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from media import persist_media as pm

FAL = "https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45LpIlkl7V6pa.jpg"
CF = "dexamplecdn03.cloudfront.net"


class FakeS3:
    def __init__(self): self.puts = []
    def put_object(self, **kw): self.puts.append(kw)
    def generate_presigned_url(self, *a, **kw): return "https://presigned.example/x"


class TestClassification:
    def test_recognises_the_real_fal_url(self):
        assert pm.media_type_for(FAL) == "image/jpeg"

    def test_extension_is_read_from_the_path_not_the_query(self):
        """A signature parameter ending in something extension-shaped must not
        make an arbitrary endpoint look like media."""
        assert pm.media_type_for("https://h/api?sig=abc.jpg") is None
        assert pm.media_type_for("https://h/a.jpg?sig=abc") == "image/jpeg"

    def test_non_media_is_rejected(self):
        for u in ("https://h/page.html", "https://h/data.zip", "https://h/"):
            assert pm.media_type_for(u) is None

    def test_our_own_urls_are_recognised(self, monkeypatch):
        """Without this an asset we already own is re-copied on every turn that
        mentions it. Scoped to OUR bucket/domain only — a FOREIGN S3 bucket (e.g. the
        M2C proxy, whose presigned urls expire) must NOT count as ours, or the render
        mirror skips it and the inline player breaks after ~1h."""
        monkeypatch.setenv("SKILLS_S3_BUCKET", "b")
        assert pm.is_already_ours(f"https://{CF}/artifacts/a.png", CF)                    # our CloudFront
        assert pm.is_already_ours("https://b.s3.us-west-2.amazonaws.com/k.png", CF)       # our bucket
        assert not pm.is_already_ours("https://other.s3.us-west-2.amazonaws.com/k.png", CF)  # foreign S3 -> mirror
        assert not pm.is_already_ours(FAL, CF)


class TestExtraction:
    def test_finds_urls_nested_anywhere(self):
        result = {"content": [{"text": f"Generated: {FAL}"}],
                  "meta": {"images": [{"url": FAL}]}}
        assert pm.external_media_urls(result, CF) == [FAL]

    def test_deduplicates_across_summary_and_structured_fields(self):
        assert len(pm.external_media_urls([FAL, FAL, FAL], CF)) == 1

    def test_excludes_our_own_and_non_media(self):
        urls = pm.external_media_urls(
            [FAL, f"https://{CF}/artifacts/mine.png", "https://h/page.html"], CF)
        assert urls == [FAL]

    def test_trailing_punctuation_is_stripped(self):
        assert pm.external_media_urls(f"see {FAL}.", CF) == [FAL]

    def test_empty_result_is_empty(self):
        for r in (None, "", {}, [], "no urls here"):
            assert pm.external_media_urls(r, CF) == []


class TestFetchGuards:
    def test_http_is_refused(self):
        with pytest.raises(pm.PersistError, match="https"):
            pm.validate_fetchable("http://example.com/a.jpg")

    def test_loopback_and_internal_hosts_refused(self):
        for u in ("https://localhost/a.jpg", "https://x.internal/a.jpg",
                  "https://y.local/a.jpg"):
            with pytest.raises(pm.PersistError):
                pm.validate_fetchable(u)

    def test_private_address_refused(self):
        with pytest.raises(pm.PersistError, match="non-public"):
            pm.validate_fetchable("https://127.0.0.1/a.jpg")

    def test_metadata_endpoint_refused(self):
        """169.254.169.254 is the reason this validation exists."""
        with pytest.raises(pm.PersistError, match="non-public"):
            pm.validate_fetchable("https://169.254.169.254/latest/meta-data/a.jpg")


class TestKey:
    def test_key_is_content_addressed(self):
        """Hosts hand out different signed URLs for identical bytes; keying on
        the URL would store the same image repeatedly."""
        body = b"same bytes"
        a = pm.key_for("https://h/a.jpg?sig=1", body, "artifacts/")
        b = pm.key_for("https://h/a.jpg?sig=2", body, "artifacts/")
        assert a == b

    def test_different_bytes_differ(self):
        assert pm.key_for(FAL, b"x", "p/") != pm.key_for(FAL, b"y", "p/")

    def test_extension_and_prefix_preserved(self):
        k = pm.key_for(FAL, b"x", "artifacts/")
        assert k.startswith("artifacts/external/") and k.endswith(".jpg")


class TestPersist:
    def test_uploads_with_the_type_from_the_extension(self, monkeypatch):
        monkeypatch.setattr(pm, "fetch", lambda u, **k: (b"IMG", "application/octet-stream"))
        s3 = FakeS3()
        out = pm.persist_url(FAL, s3_client=s3, bucket="b", prefix="artifacts/",
                             artifacts_domain=CF)
        assert s3.puts[0]["ContentType"] == "image/jpeg", \
            "octet-stream would make the browser download instead of display"
        assert out["url"] == f"https://{CF}/{out['key']}"
        assert out["source_url"] == FAL, "provenance must be retained"
        assert out["bytes"] == 3

    def test_refuses_a_url_with_no_media_extension(self):
        with pytest.raises(pm.PersistError, match="extension"):
            pm.persist_url("https://h/page", s3_client=FakeS3(), bucket="b",
                           prefix="p/", artifacts_domain=CF)

    def test_one_failure_does_not_block_the_rest(self, monkeypatch):
        calls = {"n": 0}
        def flaky(u, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise pm.PersistError("boom")
            return b"OK", "image/jpeg"
        monkeypatch.setattr(pm, "fetch", flaky)
        s3 = FakeS3()
        out = pm.persist_result_media(
            ["https://h/a.jpg", "https://h/b.jpg"], s3_client=s3, bucket="b",
            prefix="p/", artifacts_domain=CF)
        assert len(out) == 1 and len(s3.puts) == 1

    def test_a_copy_failure_never_raises(self, monkeypatch):
        """A tool that worked must not be reported failed because mirroring did."""
        def boom(u, **k):
            raise RuntimeError("network gone")
        monkeypatch.setattr(pm, "fetch", boom)
        assert pm.persist_result_media(
            [FAL], s3_client=FakeS3(), bucket="b", prefix="p/",
            artifacts_domain=CF) == []

    def test_limit_is_respected(self, monkeypatch):
        monkeypatch.setattr(pm, "fetch", lambda u, **k: (b"X", "image/jpeg"))
        urls = [f"https://h/{i}.jpg" for i in range(20)]
        out = pm.persist_result_media(urls, s3_client=FakeS3(), bucket="b",
                                      prefix="p/", artifacts_domain=CF, limit=3)
        assert len(out) == 3


class TestSwap:
    """swap_result_media — copy external media AND rewrite every source URL to the durable copy.

    This is the source-mirror chokepoint so the chat text and the render manifest both
    reference the permanent CloudFront URL instead of an ephemeral fal.media URL.
    """

    def test_swaps_fal_url_everywhere_in_the_result(self, monkeypatch):
        monkeypatch.setattr(pm, "fetch", lambda u, **k: (b"IMG", "image/jpeg"))
        s3 = FakeS3()
        result = {
            "status": "success",
            "content": [{"text": f"Here is your frame: {FAL}"}],   # text block (drives chat)
            "meta": {"images": [{"url": FAL}]},                     # structured (drives manifest)
        }
        new_result, copied = pm.swap_result_media(
            result, s3_client=s3, bucket="b", prefix="artifacts/", artifacts_domain=CF)

        assert len(copied) == 1
        durable = copied[0]["url"]
        assert durable.startswith(f"https://{CF}/artifacts/external/")
        # Every occurrence — text AND structured — points at the durable copy now.
        assert FAL not in json.dumps(new_result)
        assert new_result["content"][0]["text"] == f"Here is your frame: {durable}"
        assert new_result["meta"]["images"][0]["url"] == durable
        # The original object is NOT mutated (deep-copied rewrite).
        assert result["content"][0]["text"] == f"Here is your frame: {FAL}"

    def test_idempotent_rerun_leaves_ours_alone(self, monkeypatch):
        monkeypatch.setattr(pm, "fetch", lambda u, **k: (b"IMG", "image/jpeg"))
        s3 = FakeS3()
        result = {"content": [{"text": f"see {FAL}"}]}
        once, copied1 = pm.swap_result_media(
            result, s3_client=s3, bucket="b", prefix="artifacts/", artifacts_domain=CF)
        assert copied1
        twice, copied2 = pm.swap_result_media(
            once, s3_client=s3, bucket="b", prefix="artifacts/", artifacts_domain=CF)
        # Nothing external remains (already ours) — no re-copy, same object back.
        assert copied2 == [] and twice is once
        assert len(s3.puts) == 1

    def test_passthrough_on_copy_failure_keeps_original_url(self, monkeypatch):
        def boom(u, **k):
            raise RuntimeError("network gone")
        monkeypatch.setattr(pm, "fetch", boom)
        result = {"content": [{"text": f"see {FAL}"}]}
        new_result, copied = pm.swap_result_media(
            result, s3_client=FakeS3(), bucket="b", prefix="artifacts/", artifacts_domain=CF)
        # A copy failure must never rewrite to a broken/absent URL — keep the source.
        assert copied == [] and new_result is result
        assert FAL in json.dumps(new_result)

    def test_no_external_returns_the_original_object(self):
        result = {"content": [{"text": "no media here"}],
                  "url": f"https://{CF}/artifacts/mine.png"}
        new_result, copied = pm.swap_result_media(
            result, s3_client=FakeS3(), bucket="b", prefix="artifacts/", artifacts_domain=CF)
        assert copied == [] and new_result is result

    def test_longest_source_first_avoids_partial_prefix_rewrite(self, monkeypatch):
        # Two urls where one path is a prefix of the other; each must map to its own copy.
        u_short = "https://v3b.fal.media/files/b/aa/img.png"
        u_long = "https://v3b.fal.media/files/b/aa/img.png?v=2"
        bodies = {u_short: b"AAA", u_long: b"BBBB"}
        monkeypatch.setattr(pm, "fetch", lambda u, **k: (bodies[u], "image/png"))
        result = {"content": [{"text": f"{u_long} and {u_short}"}]}
        new_result, copied = pm.swap_result_media(
            result, s3_client=FakeS3(), bucket="b", prefix="artifacts/", artifacts_domain=CF)
        text = new_result["content"][0]["text"]
        assert "v3b.fal.media" not in text  # both fully swapped, no partial leftovers
        assert len(copied) == 2
