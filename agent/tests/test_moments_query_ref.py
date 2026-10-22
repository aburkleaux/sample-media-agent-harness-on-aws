"""The Moments tab's typed query (design 2026-10-18 §2): an image search reaches the tab as its query chip.

The search tool records the call's query image in the ledger; turn end turns the first query-bearing call into
``query_ref`` (text, or up to three images labelled from the hits they came from); the manifest carries it as
``galleryMoments.queryRef`` (typed fields only, never a URL) beside the old text ``query``.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.ledger import SearchLedgerEntry  # noqa: E402
from project_search.session_hits import SessionHit  # noqa: E402
from project_search.tool import _query_image_of  # noqa: E402
from project_search.turn_end import query_ref_for  # noqa: E402
from render.moments import MomentSelection, build_moments_manifest  # noqa: E402

PID = "p" + "a" * 32
IMG = "image:11111111-1111-4111-8111-111111111111"
VID = "video:22222222-2222-4222-8222-222222222222"
FRAME = "micro-workflow/shot-segmentation/run1/frames/frame_0003.jpg"


def _entry(term="", image=None):
    return SearchLedgerEntry("t1", "u1", PID, True, [], search_term=term, query_image=image)


def _hit(asset_id, start_ms, frame=None):
    return SessionHit(hit={"asset_id": asset_id, "start_ms": start_ms, "end_ms": start_ms + 4000,
                           "frame_s3_key": frame}, project_id=PID, turn_id="t0", tool_use_id="u0", index=0,
                      recency=(0, 0, 0))


ASSETS = {IMG: ResolvedAsset(IMG, "image", "dock_still.jpg", "indexed"),
          VID: ResolvedAsset(VID, "video", "tidal_line_trailer.mp4", "indexed")}


def test_the_tool_records_the_query_image_as_the_model_passed_it():
    assert _query_image_of({"search_image_asset_id": IMG}) == {"assetId": IMG}
    assert _query_image_of({"search_image_s3uri": f"s3://proxy/{FRAME}"}) == {"s3uri": f"s3://proxy/{FRAME}"}
    assert _query_image_of({"search_term": "dock"}) is None
    assert _query_image_of({"search_image_asset_id": "not-an-id"}) is None


def test_a_text_search_is_a_text_query():
    assert query_ref_for([_entry("title card"), _entry(image={"assetId": IMG})], PID, [], ASSETS) == \
        {"type": "text", "q": "title card"}


def test_an_image_asset_search_is_labelled_with_its_name():
    ref = query_ref_for([_entry(image={"assetId": IMG})], PID, [], ASSETS)
    assert ref == {"type": "image", "images": [{"assetId": IMG, "label": "dock_still.jpg"}], "match": "any"}


def test_a_rendition_uri_names_its_image_asset():
    uri = f"s3://ingest/projects/{PID}/{IMG}/rendition.jpg"
    ref = query_ref_for([_entry(image={"s3uri": uri})], PID, [], ASSETS)
    assert ref["images"] == [{"assetId": IMG, "label": "dock_still.jpg"}]


def test_a_frame_search_is_labelled_from_the_hit_it_came_from():
    ref = query_ref_for([_entry(image={"s3uri": f"s3://proxy/{FRAME}"})], PID, [_hit(VID, 12_400, FRAME)], ASSETS)
    assert ref["images"] == [{"frameKey": FRAME, "sourceAssetId": VID, "atMs": 12_400,
                              "label": "Frame 00:12 · tidal_line_trailer.mp4"}]


def test_up_to_three_distinct_images_in_call_order():
    imgs = [{"assetId": IMG}, {"assetId": IMG}] + [{"s3uri": f"s3://proxy/f{i}.jpg"} for i in range(5)]
    ref = query_ref_for([_entry(image=i) for i in imgs], PID, [], ASSETS)
    assert [i.get("assetId") or i.get("frameKey") for i in ref["images"]] == [IMG, "f0.jpg", "f1.jpg"]


def test_no_search_no_query():
    assert query_ref_for([], PID, [], ASSETS) is None


def _selection():
    return MomentSelection(basis="searched", project_id=PID, items=[], truncated=False)


def test_the_manifest_carries_query_ref_beside_the_old_text_query():
    ref = {"type": "image", "images": [{"frameKey": FRAME, "sourceAssetId": VID, "atMs": 12_400, "label": "Frame 00:12",
                                        "url": "https://x/y?sig=1"}], "match": "any"}
    gm = build_moments_manifest(_selection(), session_id="s", turn_id="t", created_at="z", query_ref=ref)["galleryMoments"]
    assert gm["queryRef"] == {"type": "image", "match": "any",
                              "images": [{"frameKey": FRAME, "sourceAssetId": VID, "atMs": 12_400, "label": "Frame 00:12"}]}
    assert "://" not in json.dumps(gm)  # typed fields only: a stray URL never reaches the manifest
    text = build_moments_manifest(_selection(), session_id="s", turn_id="t", created_at="z", query="dock",
                                  query_ref={"type": "text", "q": "dock"})["galleryMoments"]
    assert text["query"] == "dock" and text["queryRef"] == {"type": "text", "q": "dock"}
    bare = build_moments_manifest(_selection(), session_id="s", turn_id="t", created_at="z")["galleryMoments"]
    assert "queryRef" not in bare
