"""DEV-ONLY spike: ingest ONE existing S3 video into a hybrid-search Project collection on M2C, then search it.

Not product code. Exercises media-harness-project-ingestion Req 4-6 by hand against the shared staging M2C
gateway, so the real Ingestion_Pipeline can be built on measured behaviour.

    cd agent && AWS_PROFILE=<profile> uv run python scripts/ingest_spike.py \
        [--source s3://bucket/key.mp4] [--collection <id>] [--phrase "..."] [--visual "..."]

Pipeline:
  1. shot-segmentation + speech-diarization, submitted together (Req 4.4)
  2. media-processing JPEG frame capture (spike-only step, see FRAMES below)
  3. shot-metadata-enrichment (shots + transcript) and conversation-analysis (transcript) (Req 4.8/4.9)
  4. Index_Documents built by the validated docbuilder, every doc checked by hybrid-search validate_contract
  5. create_collection (only if absent) + inline index_content chunks of <=100, each polled to SUCCEEDED (Req 6.3)
  6. search_content: a spoken phrase and a visual description

FRAMES: shot-segmentation writes each shot's selected frames only as a tar.gz (`frames_key`) plus a tiny QC
strip (`shot_images/`), so there is no single-frame JPEG to point a `shot` document at. This spike asks
media-processing to capture JPEG frames (M2C writes them to its own proxy bucket) and picks, per shot, the
captured frame nearest the shot's first selected frame inside the shot's span.

Resumable: job ids and outcomes live in --state (default /tmp/ingest-spike/state.json), keyed by source URI,
so a rerun polls / reuses finished work instead of resubmitting. M2C submissions are also idempotent on their
parameters, so an accidental resubmit absorbs into the existing run.

Writes: M2C workflow runs, one create_collection (skipped when the collection already exists), index_content.
Reads: S3 GetObject on M2C outputs (the proxy bucket). Never prints tokens, secrets, or presigned URLs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

M2C_URL = "https://media-workflow-gateway-v6-staging-examplegw2.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
M2C_SECRET = ("arn:aws:secretsmanager:us-west-2:100000000002:secret:"
              "/media-workflow-gateway-v6/staging/gateway-credentials-EXMPL2")
M2C_TOKEN_URL = "https://media-workflow-gateway-v6-staging-100000000002.auth.us-west-2.amazoncognito.com/oauth2/token"
M2C_CLIENT_ID = "1exampleclientid0000000001"
M2C_SCOPE = "media-workflows/invoke"
REGION = "us-west-2"

DEFAULT_SOURCE = ("s3://mediaworks-harness-skills-store-100000000002-us-west-2/artifacts/"
                  "df51db2b-65d3-4573-a791-9e72823f289d/input/1787928383597-evil_santa_trailer_enUS.mp4")
DEFAULT_COLLECTION = "p28633bda7cc848b6a92eb17145918d74"
DEFAULT_PROJECT_NAME = "Trailer cuts"

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALIDATION = ROOT / "docs/prototypes/media-harness-projects/validation"
DEFAULT_RESULTS = VALIDATION / "ingest_spike_results.json"

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
# Workflow name -> (submit tool, poll tool)
TOOLS = {
    "shot-segmentation": ("shot-segmentation___segment_shots", "shot-segmentation___get_job_status"),
    "speech-diarization": ("speech-diarization___diarize_speech", "speech-diarization___get_job_status"),
    "frames": ("media-processing___process_media", "media-processing___get_job_status"),
    "shot-metadata-enrichment": ("shot-metadata-enrichment___enrich_shot_metadata",
                                 "shot-metadata-enrichment___get_job_status"),
    "conversation-analysis": ("conversation-analysis___analyze_conversation",
                              "conversation-analysis___get_job_status"),
}

logging.getLogger("mcp").setLevel(logging.ERROR)
# mcp 2.x's ClientSession logs as "client": every call warns "Tool X not listed by server" because a fresh session
# calls without a tools/list first. Harmless here (no structured-output validation needed).
logging.getLogger("client").setLevel(logging.ERROR)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load("docbuilder", VALIDATION / "docbuilder.py")
C = _load("hs_contracts", ROOT / "hybrid-search-on-aws/lib/contracts.py")


class Schema:
    """Same shape test_v3_documents.py hands validate_contract (hybrid-search default_{scope}.json)."""

    def __init__(self, scope: str):
        raw = json.loads((ROOT / f"hybrid-search-on-aws/schema/default_{scope}.json").read_text())
        self.doc_type = raw["doc_type"]
        self._mappings = raw["mappings"]
        self.ann_field = next(k for k, v in raw["mappings"].items() if v.get("ann"))
        self.required = [k for k, v in raw["mappings"].items() if v.get("required")]


SCHEMAS = {s: Schema(s) for s in ("shot", "scene", "utterance", "topic")}


def validate(docs: list[dict], collection_id: str) -> None:
    """validate_contract + the required-column check, on every doc, before anything is submitted (Req 5.16)."""
    for i, d in enumerate(docs):
        sch = SCHEMAS[d["doc_type"]]
        try:
            norm = C.validate_contract(d, sch, collection_id)
        except ValueError as e:
            raise SystemExit(f"document build: contract error at #{i} ({d['doc_type']}): {e}")
        row = {"asset_id": norm["asset_id"], **norm["fields"]}
        for it in norm["content"]:
            if it["field"] != sch.ann_field:
                row[it["field"]] = it["value"]
        row[sch.ann_field] = [0.0]
        row["start_ms"], row["end_ms"], row["doc_id"] = norm["start_ms"], norm["end_ms"], norm["doc_id"]
        missing = [f for f in sch.required if row.get(f) in (None, "", [])]
        if missing:
            raise SystemExit(f"document build: #{i} ({d['doc_type']}) missing required {missing}")


# ----------------------------------------------------------------------------------------------- M2C client
class M2C:
    def __init__(self):
        self._tok, self._exp = None, 0.0

    def token(self) -> str:
        if self._tok and time.time() < self._exp - 120:
            return self._tok
        import boto3
        sec = json.loads(boto3.client("secretsmanager", region_name=REGION)
                         .get_secret_value(SecretId=M2C_SECRET)["SecretString"])
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": sec.get("clientId") or sec.get("client_id") or M2C_CLIENT_ID,
            "client_secret": sec.get("clientSecret") or sec.get("client_secret"),
            "scope": M2C_SCOPE}).encode()
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            M2C_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=15).read())
        self._tok, self._exp = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self._tok

    async def calls(self, batch: list[tuple[str, dict]]) -> list[dict]:
        """Run several tool calls on one fresh session. Each result is the parsed JSON text (or {'_error': ...})."""
        import httpx2
        from mcp import ClientSession
        from mcp.client.client import negotiate_auto
        from mcp.client.streamable_http import streamable_http_client
        tok = await asyncio.to_thread(self.token)
        out = []
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"},
                                      timeout=httpx2.Timeout(90)) as hc:
            async with streamable_http_client(M2C_URL, http_client=hc) as (r, w, *_):
                async with ClientSession(r, w) as s:
                    await negotiate_auto(s)
                    for name, args in batch:
                        res = await s.call_tool(name, args)
                        text = "\n".join(getattr(c, "text", "") for c in res.content or [])
                        is_err = getattr(res, "is_error", None)
                        if is_err is None:
                            is_err = getattr(res, "isError", False)
                        try:
                            val = json.loads(text)
                        except ValueError:
                            val = {"_text": text}
                        if is_err:
                            val = {"_error": text[:2000]}
                        out.append(val)
        return out

    async def call(self, name: str, args: dict, attempts: int = 3) -> dict:
        """One call, retried on transport errors up to 2 more times 30 s apart (Req 4.20)."""
        for n in range(1, attempts + 1):
            try:
                return (await asyncio.wait_for(self.calls([(name, args)]), timeout=120))[0]
            except Exception as e:  # transport / throttling / timeout
                if n == attempts:
                    raise
                log(f"  {name}: transport error ({type(e).__name__}: {str(e)[:160]}), retry {n}/{attempts - 1} in 30s")
                await asyncio.sleep(30)
        raise AssertionError


# ------------------------------------------------------------------------------------------------ state
class State:
    def __init__(self, path: pathlib.Path, source: str):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"assets": {}, "collections": {}}
        self.asset = self.data["assets"].setdefault(source, {"jobs": {}})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str))
        os.replace(tmp, self.path)


# ------------------------------------------------------------------------------------------------- S3 read
_s3 = None


def s3_read(uri: str) -> bytes:
    global _s3
    import boto3
    _s3 = _s3 or boto3.client("s3", region_name=REGION)
    bucket, key = uri[5:].split("/", 1)
    return _s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def output_uri(result: dict, label: str, workflow: str) -> str:
    """Select an output by its documented label (Req 4.11)."""
    for o in result.get("outputs") or []:
        if o.get("label") == label:
            return o["uri"]
    raise SystemExit(f"{workflow}: succeeded but has no output labeled {label!r}")


def read_json(result: dict, label: str, workflow: str):
    uri = output_uri(result, label, workflow)
    try:
        raw = s3_read(uri).decode("utf-8")
        if uri.endswith(".jsonl"):
            return [json.loads(ln) for ln in raw.split("\n") if ln.strip()]
        return json.loads(raw)
    except Exception as e:
        raise SystemExit(f"{workflow}: output {label!r} unreadable: {type(e).__name__}: {e}")


# ------------------------------------------------------------------------------------------ job helpers
async def submit(m2c: M2C, st: State, wf: str, args: dict) -> None:
    jobs = st.asset["jobs"]
    if wf in jobs and jobs[wf].get("job_id"):
        log(f"{wf}: reusing {jobs[wf]['status']} job from state")
        return
    tool = TOOLS[wf][0]
    log(f"{wf}: submitting {tool}")
    res = await m2c.call(tool, args)
    if "_error" in res or not res.get("job_id"):
        jobs[wf] = {"status": "SUBMIT_FAILED", "error": res.get("_error") or json.dumps(res)[:1000],
                    "args": args, "submitted_at": time.time()}
        st.save()
        log(f"{wf}: submission rejected: {jobs[wf]['error'][:300]}")
        return
    jobs[wf] = {"job_id": res["job_id"], "status": res.get("status", "RUNNING"), "args": args,
                "submitted_at": time.time(), "absorbed": bool(res.get("message")),
                "workflow_id": res.get("workflow_id"), "output_s3_prefix": res.get("output_s3_prefix")}
    if res.get("message"):
        log(f"{wf}: absorbed into existing run ({res.get('status')}): {res['message'][:160]}")
    st.save()


async def poll(m2c: M2C, st: State, wfs: list[str], interval: int, deadline: float) -> None:
    """Poll the named workflows until each is terminal (or failed to submit)."""
    jobs = st.asset["jobs"]
    fails = {wf: 0 for wf in wfs}
    while True:
        pending = [wf for wf in wfs if wf in jobs and jobs[wf].get("job_id") and not jobs[wf].get("result")]
        if not pending:
            return
        if time.time() > deadline:
            for wf in pending:
                jobs[wf]["status"] = "SPIKE_TIMEOUT"
                jobs[wf]["result"] = {"status": "SPIKE_TIMEOUT"}
            st.save()
            log(f"timeout cap reached while waiting on {pending}")
            return
        try:
            results = await asyncio.wait_for(
                m2c.calls([(TOOLS[wf][1], {"job_id": jobs[wf]["job_id"]}) for wf in pending]), timeout=180)
        except Exception as e:
            results = [{"_error": f"transport: {type(e).__name__}: {e}"}] * len(pending)
        for wf, res in zip(pending, results):
            j = jobs[wf]
            if "_error" in res:
                fails[wf] += 1
                log(f"  {wf}: status read failed ({fails[wf]}/3): {res['_error'][:200]}")
                if fails[wf] >= 3:  # Req 7.8
                    j["status"], j["result"] = "POLL_FAILED", {"status": "POLL_FAILED", "error": res["_error"]}
                continue
            fails[wf] = 0
            status = res.get("status", "?")
            j["status"] = status
            if status in TERMINAL:
                j["result"], j["finished_at"] = res, time.time()
                log(f"  {wf}: {status} after {j['finished_at'] - j['submitted_at']:.0f}s"
                    + (f" error={str(res.get('error'))[:300]}" if status != "SUCCEEDED" else ""))
            else:
                log(f"  {wf}: {status} [{res.get('current_state', '')}] "
                    f"{time.time() - j['submitted_at']:.0f}s")
        st.save()
        if any(not jobs[wf].get("result") for wf in pending):
            await asyncio.sleep(interval)


def ok(st: State, wf: str) -> dict | None:
    j = st.asset["jobs"].get(wf) or {}
    r = j.get("result") or {}
    return r if r.get("status") == "SUCCEEDED" else None


# -------------------------------------------------------------------------------------- output adapters
def _strs(items, *keys) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                if it.get(k):
                    out.append(str(it[k]))
                    break
    return [s for s in out if s.strip()]


def _hms_ms(v) -> float:
    if isinstance(v, (int, float)):
        return float(v) * 1000.0
    h, m, s = str(v).split(":")
    return (int(h) * 3600 + int(m) * 60 + float(s)) * 1000.0


def pick_frames(shots: list[dict], manifest: dict, manifest_uri: str) -> dict[int, str]:
    """shot_id -> s3 URI of the captured frame nearest the shot's first selected frame, inside the shot.

    media-processing names frames frame_{N:07d}.jpg with N the SOURCE frame number, the same numbering as
    shots.json start_frame/end_frame/selected_frames, so the join is exact. `prefix` is a bucket key.
    """
    bucket = manifest_uri[5:].split("/", 1)[0]
    prefix = manifest["prefix"].rstrip("/")
    if prefix.startswith("s3://"):
        prefix = prefix[5:].split("/", 1)[1]
    frames = sorted((int(pathlib.Path(k).stem.split("_")[-1]), f"s3://{bucket}/{prefix}/{k}")
                    for k in manifest.get("keys") or [])
    out = {}
    for sh in shots:
        s, e = int(sh["start_frame"]), int(sh["end_frame"])
        sel = sh.get("selected_frames") or []
        target = sel[0] if sel else (s + e) // 2
        inside = [f for f in frames if s <= f[0] <= e]
        if inside:
            out[sh["shot_id"]] = min(inside, key=lambda f: (abs(f[0] - target), f[0]))[1]
    return out


def shots_input(shots_json: dict, frame_for: dict[int, str]) -> list[dict]:
    return [{"start": float(sh["start_time"]) * 1000, "end": float(sh["end_time"]) * 1000,
             "frame": frame_for.get(sh["shot_id"], "")} for sh in shots_json["shots"]]


def enrich_input(shots_json: dict, shot_md: list[dict]) -> list[dict]:
    times = {sh["shot_id"]: sh for sh in shots_json["shots"]}
    out = []
    for md in shot_md:
        sh = times.get(md.get("shot_id"))
        if not sh:
            continue
        out.append({
            "start": float(sh["start_time"]) * 1000, "end": float(sh["end_time"]) * 1000,
            "description": md.get("description") or "",
            "objects": _strs(md.get("objects"), "name"),
            "setting": md.get("setting") or "",
            "on_screen_text": ", ".join(_strs(md.get("texts_on_screen"), "text", "value", "content")),
            "brands": _strs(md.get("brands"), "name"),
            "sentiment": md.get("sentiment") or "",
            "entities": _strs(md.get("named_entities"), "name", "text") + _strs(md.get("people"), "name"),
        })
    return out


def diar_input(transcript: dict) -> list[dict]:
    return [{"start": float(s["start"]) * 1000, "end": float(s["end"]) * 1000, "text": s.get("text") or "",
             "speaker": s.get("speaker_id")} for s in transcript.get("sentences") or []]


def topic_summary(seg: dict) -> str:
    """Topic text = the segment's topic title plus its key events (conversation-analysis has no `summary`)."""
    title = (seg.get("segment_topic") or "").strip()
    events = "; ".join(_strs(seg.get("key_events")))
    return f"{title}. {events}".strip() if title and events else (title or events)


def topics_input(analysis: dict) -> list[dict]:
    return [{"start": _hms_ms(s["start_time"]), "end": _hms_ms(s["end_time"]), "summary": topic_summary(s),
             "entities": _strs(s.get("named_entities"), "name")} for s in analysis.get("audio_segments") or []]


# ------------------------------------------------------------------------------------------------- main
async def run(a) -> int:
    m2c = M2C()
    st = State(pathlib.Path(a.state), a.source)
    jobs = st.asset["jobs"]
    deadline = time.time() + a.timeout_min * 60
    name = a.source.rsplit("/", 1)[-1]
    short = hashlib.sha256(a.source.encode()).hexdigest()[:12]
    asset_id = f"video:{short}"
    st.asset.update({"asset_id": asset_id, "title": name})
    st.save()
    log(f"source={a.source} asset_id={asset_id} collection={a.collection}")

    # 1. shot-segmentation + speech-diarization, neither waits for the other (Req 4.4). Default parameters.
    await submit(m2c, st, "shot-segmentation", {"asset_s3uri": a.source})
    await submit(m2c, st, "speech-diarization", {"asset_s3uri": a.source})
    # Spike-only: frames depend on the source alone, so they run alongside wave 1.
    await submit(m2c, st, "frames", {
        "asset_s3uri": a.source, "width": a.frame_width, "height": a.frame_height,
        "output_groups": [{"group_name": "frames", "format": "jpeg", "frame_interval_sec": a.frame_interval}]})
    await poll(m2c, st, ["shot-segmentation", "speech-diarization"], a.poll, deadline)
    seg_r, dia_r = ok(st, "shot-segmentation"), ok(st, "speech-diarization")
    if not seg_r:
        log("shot-segmentation did not succeed; cannot build shot/scene documents")
    shots_json = read_json(seg_r, "shot-list", "shot-segmentation") if seg_r else None
    D = int(B._r(float(shots_json.get("actual_duration_sec") or 0) * 1000)) if shots_json else 0
    if shots_json and D <= 0:
        raise SystemExit("shot-segmentation: video duration is missing or invalid")  # Req 5.12

    transcript = read_json(dia_r, "transcript", "speech-diarization") if dia_r else None
    transcript_uri = output_uri(dia_r, "transcript", "speech-diarization") if dia_r else None
    has_speech = bool(transcript and any((s.get("text") or "").strip() for s in transcript.get("sentences") or []))
    log(f"duration={D}ms shots={len(shots_json['shots']) if shots_json else 0} "
        f"sentences={len(transcript.get('sentences') or []) if transcript else 0} speech={has_speech}")

    # 2. frames (spike-only) + 3. enrichment / conversation-analysis
    wave2 = ["frames"]
    if shots_json:
        run_prefix = seg_r.get("output_s3_prefix") or output_uri(seg_r, "shot-list", "shot-segmentation")
        enr = {"shot_segmentation_s3uri": run_prefix.rstrip("/"), "use_batch": a.batch,
               "history_scope": f"asset.{asset_id.replace(':', '_')}"}
        if has_speech:
            enr["transcript_s3uri"] = transcript_uri
        await submit(m2c, st, "shot-metadata-enrichment", enr)
        wave2.append("shot-metadata-enrichment")
    if has_speech:
        await submit(m2c, st, "conversation-analysis", {
            "transcript_s3uri": transcript_uri, "history_scope": f"asset.{asset_id.replace(':', '_')}"})
        wave2.append("conversation-analysis")
    await poll(m2c, st, wave2, a.poll, deadline)

    # 4. Index_Documents
    seg_in, enr_in, diar_in, top_in = [], [], [], []
    if shots_json and ok(st, "frames"):
        fr = ok(st, "frames")
        manifest = read_json(fr, "frames", "media-processing")  # label = the jpeg group_name
        frame_for = pick_frames(shots_json["shots"], manifest, output_uri(fr, "frames", "media-processing"))
        st.asset["frames"] = {"captured": len(manifest.get("keys") or []), "interval_s": a.frame_interval,
                              "dimension": manifest.get("dimension"), "shots_with_frame": len(frame_for)}
        seg_in = shots_input(shots_json, frame_for)
    elif shots_json:
        log("frames did not succeed; no shot documents")
    if ok(st, "shot-metadata-enrichment"):
        enr_in = enrich_input(shots_json, read_json(ok(st, "shot-metadata-enrichment"), "shots",
                                                    "shot-metadata-enrichment"))
    if has_speech:
        diar_in = diar_input(transcript)
    if ok(st, "conversation-analysis"):
        top_in = topics_input(read_json(ok(st, "conversation-analysis"), "analysis", "conversation-analysis"))
    if not D:
        D = int(B._r(float((transcript or {}).get("audio_duration_sec") or 0) * 1000))
    docs, omitted = B.build(asset_id, "video", seg_in, enr_in, diar_in, top_in, D)
    validate(docs, a.collection)
    record = B.to_jsonl(docs)
    digest = hashlib.sha256(record.encode()).hexdigest()
    counts = {t: sum(d["doc_type"] == t for d in docs) for t in ("shot", "scene", "utterance", "topic")}
    st.asset.update({"doc_counts": counts, "omitted": omitted, "record_sha256": digest})
    st.save()
    out_dir = pathlib.Path(a.state).parent
    (out_dir / f"{short}.documents.jsonl").write_text(record)
    log(f"built {len(docs)} docs {counts} omitted={omitted}; all pass validate_contract")
    if not docs:
        raise SystemExit("document build: the asset produced no indexable content")  # Req 5.17
    if a.stop_after == "build":
        log("--stop-after build: not touching the collection")
        write_results(a, st, [])
        return 0

    # 5. collection + index
    coll = st.data["collections"].setdefault(a.collection, {})
    if not coll.get("ready"):
        listed = await m2c.call("hybrid-search___list_collections", {})
        existing = next((c for c in listed.get("collections") or [] if c.get("collection_id") == a.collection), None)
        if existing:
            log(f"collection {a.collection} already exists (scopes {existing.get('active_scopes')}); not creating")
            coll.update({"ready": True, "created_by_spike": coll.get("created_by_spike", False),
                         "active_scopes": existing.get("active_scopes")})
        else:
            default = next((c for c in listed.get("collections") or [] if c.get("collection_id") == "default"), {})
            scopes = default.get("active_scopes") or ["shot", "scene", "utterance", "topic"]
            log(f"create_collection {a.collection} scopes={scopes}")
            res = await m2c.call("hybrid-search___create_collection",
                                 {"collection_id": a.collection, "active_scopes": scopes, "name": a.project_name})
            if "_error" in res and "exist" not in res["_error"].lower():
                raise SystemExit(f"indexing: create_collection failed: {res['_error'][:500]}")
            coll.update({"ready": True, "created_by_spike": True, "active_scopes": scopes, "create_response": res})
        st.save()

    idx = st.asset.setdefault("index", {})
    if idx.get("completed_sha256") == digest and idx.get("collection") == a.collection:
        log("index: identical Document_Record already indexed; skipping submission")
    else:
        first = not idx.get("accepted_any")
        chunks = B.chunks(docs, first_submission=first, identical_to_last=False)
        idx.update({"collection": a.collection, "started_sha256": digest})
        idx.setdefault("failed_attempts", []).extend(c for c in idx.pop("chunks", []) if c.get("status") != "SUCCEEDED")
        idx["chunks"] = []
        totals = {"prepared": 0, "skipped": 0, "indexed": 0}
        for i, ch in enumerate(chunks):
            log(f"index_content chunk {i + 1}/{len(chunks)} ({len(ch)} docs, refresh={any(d['refresh'] for d in ch)})")
            res = await m2c.call("hybrid-search___index_content", {
                "collection_id": a.collection, "documents": ch, "asset_metadata": {"title": name}}, attempts=1)
            if "_error" in res or not res.get("job_id"):
                idx["chunks"].append({"error": res.get("_error") or res})
                st.save()
                raise SystemExit(f"indexing: index_content chunk {i + 1} rejected: {str(res)[:500]}")
            c = {"job_id": res["job_id"], "submitted_at": time.time(), "n": len(ch)}
            idx["chunks"].append(c)
            st.save()
            while True:
                await asyncio.sleep(10)
                r = await m2c.call("hybrid-search___get_job_status", {"job_id": c["job_id"]})
                s = r.get("status")
                if s in TERMINAL:
                    break
                log(f"  chunk {i + 1}: {s or r.get('_error', '')[:200]}")
                if time.time() > deadline:
                    raise SystemExit("indexing: timeout cap reached")
            c.update({"status": s, "finished_at": time.time(),
                      **{k: r.get(k) for k in ("prepared", "skipped", "indexed", "error")}})
            st.save()
            log(f"  chunk {i + 1}: {s} prepared={r.get('prepared')} skipped={r.get('skipped')} "
                f"indexed={r.get('indexed')} {str(r.get('error') or '')[:300]}")
            if s != "SUCCEEDED":
                raise SystemExit(f"indexing: chunk {i + 1} {s}")
            # Only a SUCCEEDED chunk can have written rows, so only it makes a later resubmit a refresh (Req 6.4/6.6).
            idx["accepted_any"] = True
            for k in totals:
                totals[k] += int(r.get(k) or 0)
        idx.update({"completed_sha256": digest, "totals": totals})
        st.save()

    # 6. search
    phrase = a.phrase or pick_phrase(transcript)
    visual = a.visual or pick_visual(enr_in)
    searches = []
    # The spoken phrase runs twice: plain, and with the `says:` dialogue operator. Measured on the first run, the
    # plain form is routed to the visual/scene ANN paths only and no utterance reaches the top 20.
    queries = [("spoken_phrase_plain", phrase), ("spoken_phrase_says", f'says:"{phrase}"' if phrase else None),
               ("visual_description", visual)]
    for label, q in queries:
        if not q:
            continue
        t0 = time.time()
        res = await m2c.call("hybrid-search___search_content", {"collection_id": a.collection,
                                                                "search_term": q, "k": a.k})
        dt = time.time() - t0
        hits = [compact(h) for h in res.get("results") or []]
        searches.append({"label": label, "query": q, "latency_s": round(dt, 1), "total": res.get("total"),
                         "skipped_indices": res.get("skipped_indices"), "error": res.get("_error"),
                         "hits": hits})
        print(f"\n=== search ({label}): {q!r}  [{dt:.1f}s, total={res.get('total')}, "
              f"skipped_indices={res.get('skipped_indices')}]")
        if res.get("_error"):
            print("  ERROR:", res["_error"][:500])
        for h in hits:
            print(f"  {h['final_score']:.3f} {h['doc_type']:<9} {h['asset_id']} "
                  f"{h['start_ms']}-{h['end_ms']}ms  {h['snippet']}")

    write_results(a, st, searches)
    return 0


def compact(h: dict) -> dict:
    snippet = (h.get("text") or "").replace("\n", " | ")[:160] or f"[frame] {h.get('frame_s3_key', '')}"
    return {"doc_type": h.get("source_index"), "asset_id": h.get("asset_id"), "start_ms": h.get("start_ms"),
            "end_ms": h.get("end_ms"), "final_score": h.get("final_score") or 0.0, "title": h.get("title"),
            "match_sources": h.get("match_sources"), "snippet": snippet}


def pick_phrase(transcript: dict | None) -> str | None:
    """A distinctive spoken sentence: the longest sentence of 5-14 words."""
    sents = [(s.get("text") or "").strip() for s in (transcript or {}).get("sentences") or []]
    cands = [s for s in sents if 5 <= len(s.split()) <= 14] or [s for s in sents if s]
    return max(cands, key=lambda s: len(s.split())) if cands else None


def pick_visual(enr_in: list[dict]) -> str | None:
    """A paraphrase built from structured attributes, not a copy of a scene's description text."""
    for e in sorted(enr_in, key=lambda e: -(e["end"] - e["start"])):
        if e["objects"] and e["setting"]:
            return f"{e['objects'][0]} in a {e['setting'].replace('_', ' ')}"
    return None


def write_results(a, st: State, searches: list[dict]) -> None:
    jobs = st.asset["jobs"]
    wf = {}
    for k, j in jobs.items():
        r = j.get("result") or {}
        wf[k] = {"tool": TOOLS[k][0], "job_id": j.get("job_id"), "status": j.get("status"),
                 "absorbed_existing_run": j.get("absorbed"),
                 "observed_duration_s": round(j["finished_at"] - j["submitted_at"]) if j.get("finished_at") else None,
                 "pipeline_wall_sec": r.get("pipeline_wall_sec"), "error": (r.get("error") or j.get("error")),
                 "params": {k2: v for k2, v in (j.get("args") or {}).items() if k2 != "asset_s3uri"}}
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": "agent/scripts/ingest_spike.py",
        "environment": "staging 100000000002 us-west-2, shared M2C (m2cv6-0e0000000002)",
        "source": {"s3_uri": a.source, **{k: v for k, v in (a.probe or {}).items()}},
        "asset_id": st.asset.get("asset_id"), "title": st.asset.get("title"),
        "collection": {"collection_id": a.collection, "name": a.project_name,
                       **{k: v for k, v in st.data["collections"].get(a.collection, {}).items()
                          if k in ("active_scopes", "created_by_spike")}},
        "workflows": wf,
        "frames": st.asset.get("frames"),
        "doc_counts": st.asset.get("doc_counts"), "omitted": st.asset.get("omitted"),
        "record_sha256": st.asset.get("record_sha256"),
        "index": {k: v for k, v in (st.asset.get("index") or {}).items() if k in ("chunks", "totals", "failed_attempts")},
        "searches": searches,
    }
    pathlib.Path(a.results).write_text(json.dumps(summary, indent=2, default=str) + "\n")
    log(f"wrote {a.results}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--source", default=DEFAULT_SOURCE, help="s3:// URI of the video (M2C roles must read it)")
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    p.add_argument("--state", default="/tmp/ingest-spike/state.json")
    p.add_argument("--results", default=str(DEFAULT_RESULTS))
    p.add_argument("--phrase", help="spoken-phrase query (default: picked from the transcript)")
    p.add_argument("--visual", help="visual-description query (default: built from enrichment attributes)")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--poll", type=int, default=25, help="poll interval seconds")
    p.add_argument("--timeout-min", type=int, default=60)
    p.add_argument("--batch", action="store_true", help="enrichment use_batch=true (half cost, much slower)")
    p.add_argument("--frame-interval", type=float, default=0.25)
    p.add_argument("--frame-width", type=int, default=960)
    p.add_argument("--frame-height", type=int, default=540)
    p.add_argument("--probe-json", help="optional JSON of source facts (duration_s, size_bytes, ...) to record")
    p.add_argument("--stop-after", choices=["build"], help="stop after building/validating documents")
    a = p.parse_args()
    if not a.source.startswith("s3://"):
        p.error("--source must be an s3:// URI")
    a.probe = json.loads(a.probe_json) if a.probe_json else None
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
