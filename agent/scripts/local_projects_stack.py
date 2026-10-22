"""DEV ONLY local Projects stack: a moto DynamoDB Projects table plus a local Token_Issuer.

Lets the local harness UI run Project search end to end with NO AWS writes:

  * ``moto``   - a moto DynamoDB server on 127.0.0.1:5005 with table ``mediaworks-harness-projects-local``
                 (PK/SK strings, PAY_PER_REQUEST). The proxy's Project_Registry writes go here.
  * ``issuer`` - an HTTP server on 127.0.0.1:8098 with ``POST /issue`` and ``POST /refresh`` that wrap the
                 REAL ``index.make_issue_handler`` / ``index.make_refresh_handler`` from
                 ``infra/lib/lambdas/token-issuer`` with dev pieces: ``dev_login.DevLoginUserTokenVerifier``
                 (the UI's Dev Login HS256 token), the moto table, and a dev RSA signer over the
                 local Search_Gateway emulator's key (``scripts/local_search_gateway.py``: same ISS / AUD /
                 KID / key file, so tokens issued here verify there). Always HTTP 200 with the handler's
                 JSON ``{status: ...}`` body.
  * ``all``    - both, in one process, and prints the env the proxy and the agents need.
  * ``seed-demo`` - write the "Demo videos" fixture Project rows into the moto Projects table and exit
                 (connects to a moto server already started by ``moto``/``all`` in another terminal).

The ``moto`` and ``all`` servers also create a second moto table, ``mediaworks-harness-plan-state-local``
(``session_id``/``node_id`` strings), beside the Projects table. ``DynamoSessionHitStore`` uses it locally
(``SESSION_HITS_TABLE``), so local Session_Hits never land in the staging plan-state table even though the
local Main_Agent runs with the staging runtime env (design Data Models, "Session_Hit_Store").

``seed-demo`` writes the fixture Project ``pfa800aa0274646bdbe9e0b69f24b6054`` ("Demo videos") whose M2C
collection already holds four indexed demo videos: the ``META`` row, the Owner (``--owner``, default
``local-dev-user``, the Dev Login sub) membership row and USER mirror, any ``--member`` rows (repeatable),
and one ``ASSET#<assetId>`` row per indexed video (status ``indexed``, kind ``video``, the real M2C asset
ids and Display_Names, sources under ``projects/<pid>/uploads/<file>``). It is idempotent: every row is a
``PutItem`` keyed by its own key, so re-running overwrites in place. It supports the local end-to-end run of
search + Q&A + Moments without any AWS writes.

Both servers bind 127.0.0.1 only. This process uses dummy AWS credentials (``testing``) and talks only to
the moto endpoint; it never reaches AWS. moto ignores SigV4 signatures, so the proxy can keep its real
``AWS_PROFILE`` (it needs it for sessions and agent runtimes) and still use this table.

  cd agent
  uv run --extra dev --with 'moto[server]' python scripts/local_projects_stack.py all
  # in another terminal, seed the Demo videos fixture Project into the running moto table:
  uv run --extra dev --with 'moto[server]' python scripts/local_projects_stack.py seed-demo
  # the search path also needs the emulator (another terminal):
  AWS_PROFILE=<profile> uv run --with cedarpy python scripts/local_search_gateway.py serve \\
      --alias-any <indexed m2c collection>
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TOKEN_ISSUER_DIR = REPO / "infra" / "lib" / "lambdas" / "token-issuer"

HOST = "127.0.0.1"
MOTO_PORT = 5005
ISSUER_PORT = 8098
TABLE = "mediaworks-harness-projects-local"
PLAN_STATE_TABLE = "mediaworks-harness-plan-state-local"
REGION = "us-west-2"
MOTO_URL = f"http://{HOST}:{MOTO_PORT}"
ISSUER_URL = f"http://{HOST}:{ISSUER_PORT}"
LIFETIME_S = 3600
MAX_BODY_BYTES = 64 * 1024

# ---- Demo videos fixture Project (design "Fixture Project"; search-qa-rendering Local end-to-end run) ----
# The staging Project whose M2C collection already holds four indexed demo videos. The collection id is the
# Project id, so a search under this Project needs no gateway alias. The owner default is the Dev Login sub.
DEMO_PROJECT_ID = "pfa800aa0274646bdbe9e0b69f24b6054"
DEMO_PROJECT_NAME = "Demo videos"
DEFAULT_OWNER_SUB = "local-dev-user"  # dev_login.py: the Dev Login HS256 token's sub
SKILLS_BUCKET = "mediaworks-harness-skills-store-100000000002-us-west-2"
# (asset_id, Display_Name) for the four indexed videos, with their real M2C asset ids.
DEMO_ASSETS = (
    ("video:41a2a608907e", "ibc_intro_olive_final.mp4"),
    ("video:95cd993da94e", "tidal_line_trailer_enUS.mp4"),
    ("video:f991228da306", "nba.mp4"),
    ("video:9dbd3f8d3dde", "Netflix_Open_Content_Meridian.mp4"),
)


def _dummy_aws_env() -> None:
    """This process only ever talks to moto: pin static dummy creds and drop any real profile."""
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        os.environ.pop(k, None)
    os.environ.update({
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_EC2_METADATA_DISABLED": "true",
    })


def _import_issuer():
    """The real issuer modules (``index``, ``access``, ``tokens``, ``dev_login``) and the emulator."""
    for p in (str(TOKEN_ISSUER_DIR), str(HERE)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import access
    import dev_login
    import index
    import local_search_gateway as gw
    import tokens
    return index, access, tokens, dev_login, gw


# ---- moto -----------------------------------------------------------------------------------------

def start_moto():
    try:
        from moto.server import ThreadedMotoServer
    except ImportError as exc:  # flask / flask-cors come with the [server] extra only
        sys.exit(f"moto server is unavailable ({exc}); run with: uv run --extra dev --with 'moto[server]' ...")
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no per-request access lines
    server = ThreadedMotoServer(ip_address=HOST, port=MOTO_PORT, verbose=False)
    server.start()
    ensure_table()
    ensure_plan_state_table()
    print(f"[local-projects] moto DynamoDB {MOTO_URL}  table={TABLE}  plan_state={PLAN_STATE_TABLE}", flush=True)
    return server


def ensure_table() -> None:
    import boto3
    ddb = boto3.client("dynamodb", endpoint_url=MOTO_URL, region_name=REGION)
    if TABLE in ddb.list_tables().get("TableNames", []):
        return
    ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"},
                              {"AttributeName": "assetListPk", "AttributeType": "S"},
                              {"AttributeName": "assetListSk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        # The Assets tab's newest-first list (`assetsByRegistered`, infra/lib/projects/projects-stack.ts): without
        # it the local proxy's asset routes fail with "Invalid index".
        GlobalSecondaryIndexes=[{
            "IndexName": "assetsByRegistered",
            "KeySchema": [{"AttributeName": "assetListPk", "KeyType": "HASH"},
                          {"AttributeName": "assetListSk", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )


def ensure_plan_state_table() -> None:
    """The local plan-state table that ``DynamoSessionHitStore`` uses for Session_Hits (same moto instance).

    Keys mirror the deployed ``<projectName>-plan-state`` table: ``session_id`` (HASH, S) + ``node_id``
    (RANGE, S). The Session_Hit_Store writes Hit items under a ``HITS#<sid>`` partition here, so local Hits
    never land in the staging plan-state table (design Data Models, "Session_Hit_Store").
    """
    import boto3
    ddb = boto3.client("dynamodb", endpoint_url=MOTO_URL, region_name=REGION)
    if PLAN_STATE_TABLE in ddb.list_tables().get("TableNames", []):
        return
    ddb.create_table(
        TableName=PLAN_STATE_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                              {"AttributeName": "node_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                   {"AttributeName": "node_id", "KeyType": "RANGE"}],
    )


# ---- seed-demo ------------------------------------------------------------------------------------

def _fold(name: str) -> str:
    """The Project_Registry name fold (``names.ts`` ``foldName``): NFC then lower-case."""
    return unicodedata.normalize("NFC", name).lower()


def seed_demo(owner_sub: str, member_subs: list[str] | None = None) -> list[dict]:
    """Write the Demo videos fixture Project rows idempotently; return the rows written.

    Writes the ``META`` row, the Owner membership row and USER mirror, one membership row and mirror per
    ``--member`` sub, and one ``ASSET#<assetId>`` row per indexed demo video. Every row is a ``PutItem``
    keyed by its own key, so re-running overwrites in place (idempotent per the task).

    The schema matches the real items: META mirrors ``registry.ts`` ``metaItem`` (the attributes
    ``access.py``/``access.ts`` read to grant access are ``status = active`` plus the Owner's ``MEMBER#``
    row); each ASSET row carries the attributes the ingestion Asset_Service's ``resolve_assets`` reads
    (``assetId``, ``kind``, ``displayName``, ``status``) plus the ingestion ASSET-row shape.
    """
    import boto3

    members = [m for m in dict.fromkeys(member_subs or []) if m and m != owner_sub]
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    pk = f"PROJECT#{DEMO_PROJECT_ID}"
    rows: list[dict] = []

    # META: Owner not counted in memberCount; four indexed assets.
    rows.append({
        "PK": pk, "SK": "META",
        "projectId": DEMO_PROJECT_ID,
        "name": DEMO_PROJECT_NAME,
        "nameFold": _fold(DEMO_PROJECT_NAME),
        "ownerSub": owner_sub,
        "personal": False,
        "status": "active",
        "memberCount": len(members),
        "createdAt": at,
        "activeAssetCount": len(DEMO_ASSETS),
        "collectionState": "ready",
        "statusCounts": {"queued": 0, "analyzing": 0, "indexing": 0,
                         "indexed": len(DEMO_ASSETS), "failed": 0, "removed": 0},
    })

    # Owner membership + USER mirror (the mirror makes the Project appear in GET /projects).
    rows.append({"PK": pk, "SK": f"MEMBER#{owner_sub}", "role": "owner",
                 "display": owner_sub, "addedAt": at, "addedBy": owner_sub})
    rows.append({"PK": f"USER#{owner_sub}", "SK": f"PROJECT#{DEMO_PROJECT_ID}", "role": "owner"})
    for m in members:
        rows.append({"PK": pk, "SK": f"MEMBER#{m}", "role": "member",
                     "display": m, "addedAt": at, "addedBy": owner_sub})
        rows.append({"PK": f"USER#{m}", "SK": f"PROJECT#{DEMO_PROJECT_ID}", "role": "member"})

    # One ASSET row per indexed demo video.
    for asset_id, display_name in DEMO_ASSETS:
        rel_key = f"projects/{DEMO_PROJECT_ID}/uploads/{display_name}"
        rows.append({
            "PK": pk, "SK": f"ASSET#{asset_id}",
            "schemaVersion": 1,
            "assetId": asset_id,
            "kind": "video",
            "source": "upload",
            "sourceUri": f"s3://{SKILLS_BUCKET}/{rel_key}",
            "s3_prefix": rel_key,
            "displayName": display_name,
            "displayFold": _fold(display_name),
            "status": "indexed",
            "registeredAt": at,
            "addedBy": owner_sub,
            "assetListPk": pk,
            "assetListSk": f"{at}#{asset_id}",
        })

    table = boto3.resource("dynamodb", endpoint_url=MOTO_URL, region_name=REGION).Table(TABLE)
    with table.batch_writer() as writer:
        for item in rows:
            writer.put_item(Item=item)

    print(f"[local-projects] seeded Project {DEMO_PROJECT_ID} ({DEMO_PROJECT_NAME}) into {TABLE}: "
          f"META, owner={owner_sub}, members={len(members)}, assets={len(DEMO_ASSETS)} "
          f"({len(rows)} rows)", flush=True)
    for item in rows:
        print(f"[local-projects]   {item['PK']} / {item['SK']}", flush=True)
    return rows


# ---- issuer ---------------------------------------------------------------------------------------

def _dev_signer_and_verifier(tokens, key, kid):
    """A dev RSA ``Signer``/``Verifier`` pair over the emulator's local key.

    ``tokens`` is now dependency-free (it signs/verifies via KMS), so a local dev wrapper supplies its
    own RSA pieces. ``cryptography`` is available in the dev extra; the deployed Lambda uses neither.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, utils

    class _LocalPemSigner:
        kid = property(lambda self: self._kid)

        def __init__(self, private_key, key_id):
            self._key = private_key
            self._kid = key_id

        def public_key(self):
            return self._key.public_key()

        def sign_digest(self, digest):
            return self._key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))

    class _LocalRsaVerifier:
        def __init__(self, pubkeys):
            self._pub = dict(pubkeys)

        def __contains__(self, k):
            return isinstance(k, str) and k in self._pub

        def verify_digest(self, k, digest, signature):
            try:
                self._pub[k].verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
                return True
            except InvalidSignature:
                return False

    return _LocalPemSigner(key, kid), _LocalRsaVerifier({kid: key.public_key()})


def build_handlers():
    """``{"/issue": handler, "/refresh": handler}`` over the real issuer code and dev pieces."""
    index, access, tokens, dev_login, gw = _import_issuer()
    key = gw._key()  # creates the emulator's signing key if absent
    signer, verifier = _dev_signer_and_verifier(tokens, key, gw.KID)
    table = access.open_table(TABLE, endpoint_url=MOTO_URL, region_name=REGION)
    return {
        "/issue": index.make_issue_handler(dev_login.DevLoginUserTokenVerifier(), table, signer,
                                           gw.ISS, gw.AUD, LIFETIME_S),
        "/refresh": index.make_refresh_handler(verifier, table, signer,
                                               gw.ISS, gw.AUD, LIFETIME_S),
    }


def start_issuer():
    handlers = build_handlers()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            fn = handlers.get(self.path.split("?", 1)[0])
            if fn is None:
                return self._send(404, {"error": "not_found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(min(max(n, 0), MAX_BODY_BYTES + 1))
            try:
                event = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else {}
            except ValueError:
                event = {}
            # The handler answers malformed events itself (unauthenticated / invalid_token / not_found).
            self._send(200, fn(event))

        def log_message(self, fmt, *args):  # method + path + status only; never a body or token
            sys.stderr.write(f"[local-projects] issuer {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    httpd = ThreadingHTTPServer((HOST, ISSUER_PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="local-issuer", daemon=True).start()
    print(f"[local-projects] token issuer {ISSUER_URL}  POST /issue  POST /refresh  lifetime={LIFETIME_S}s",
          flush=True)
    return httpd


def print_env() -> None:
    print(f"""
[local-projects] proxy env (keep your real AWS_PROFILE; moto ignores signatures, so it is used only to sign):
  export PROJECTS_TABLE={TABLE}
  export PROJECTS_DDB_ENDPOINT={MOTO_URL}
  export LOCAL_TOKEN_ISSUER_URL={ISSUER_URL}
  export SEARCH_EXECUTION_MODE=main
  (AWS_REGION must be {REGION} or unset: moto keys tables by the signing region.)

[local-projects] agent env:
  export LOCAL_TOKEN_REFRESH_URL={ISSUER_URL}
  export SESSION_HITS_TABLE={PLAN_STATE_TABLE}
  export SESSION_HITS_DDB_ENDPOINT={MOTO_URL}
""", flush=True)


def _wait_forever(stoppers) -> None:
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    print("[local-projects] running; Ctrl-C to stop", flush=True)
    done.wait()
    for stop in stoppers:
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass
    print("[local-projects] stopped", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["moto", "issuer", "all", "seed-demo"])
    ap.add_argument("--owner", default=DEFAULT_OWNER_SUB,
                    help=f"Owner sub for the seeded Demo videos Project (default {DEFAULT_OWNER_SUB})")
    ap.add_argument("--member", action="append", default=[], metavar="SUB",
                    help="A member sub to add to the seeded Project (repeatable)")
    ap.add_argument("--seed-demo", action="store_true",
                    help="With moto/all: seed the Demo videos Project after startup, then keep running")
    a = ap.parse_args()
    _dummy_aws_env()

    if a.cmd == "seed-demo":
        ensure_table()  # fails fast if no moto server is listening
        ensure_plan_state_table()
        seed_demo(a.owner, a.member)
        return

    stoppers = []
    if a.cmd in ("moto", "all"):
        stoppers.append(start_moto().stop)
    if a.cmd in ("issuer", "all"):
        if a.cmd == "issuer":
            ensure_table()  # fails fast if no moto server is listening
        httpd = start_issuer()
        stoppers.insert(0, lambda: (httpd.shutdown(), httpd.server_close()))
    if a.seed_demo:
        if a.cmd not in ("moto", "all"):
            sys.exit("--seed-demo needs a local moto server; use `moto`/`all` here, or the `seed-demo` command")
        seed_demo(a.owner, a.member)
    print_env()
    _wait_forever(stoppers)


if __name__ == "__main__":
    main()
