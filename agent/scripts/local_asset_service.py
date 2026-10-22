"""DEV ONLY: a local HTTP wrapper around the Asset_Service handler (Projects Phase B/C).

The proxy's Asset_Service client (``ui/server/projects/assetServiceClient.ts``) invokes the
``ASSET_SERVICE_FN`` Lambda in a deployed env. For a local run it instead POSTs the SAME event to
``${LOCAL_ASSET_SERVICE_URL}/invoke`` (refused by the proxy when ``NODE_ENV=production``). This script
serves that endpoint with the REAL ``ingestion.handlers.asset_service.AssetService`` over the table and
bucket you point it at, using YOUR AWS credentials (``AWS_PROFILE`` etc.):

  * ``POST /invoke`` — body = the Asset_Service event; reply = the handler's ``{ok, ...}`` JSON, HTTP 200.

Identity. Without flags the User_Token is verified exactly as the Lambda does (Cognito; needs
``USER_POOL_ID`` + ``USER_POOL_CLIENT_ID``). ``--dev-bypass`` additionally accepts, for a local
``DEV_BYPASS_AUTH=true`` proxy, an event with no ``userToken`` that carries ``devBypassActor`` (the
proxy's dev-bypass actor), and the UI's Dev Login HS256 token. That is DEV ONLY and lives only in this
script: the Lambda never reads ``devBypassActor`` and never builds these verifiers. The server binds
127.0.0.1 only.

Env: ``PROJECTS_TABLE`` (required), ``INGEST_BUCKET`` (or ``UPLOAD_BUCKET``; required),
``PROJECTS_DDB_ENDPOINT`` (optional, e.g. the moto server of ``local_projects_stack.py``),
``INGESTION_CONFIG`` (optional, for ``s3Allowlist`` / ``uploadLimits``), ``AWS_REGION`` (default us-west-2).

  cd agent
  # against staging (the opt-in E2E, ui/server/projects/projectUploadE2E.test.ts):
  AWS_PROFILE=<staging> PROJECTS_TABLE=<staging projects table> \\
    INGEST_BUCKET=mediaworks-harness-skills-store-100000000002-us-west-2 \\
    uv run python scripts/local_asset_service.py --dev-bypass
  # then, in the proxy's env:  export LOCAL_ASSET_SERVICE_URL=http://127.0.0.1:8099
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_SRC = HERE.parent / "src"
TOKEN_ISSUER_DIR = HERE.parents[1] / "infra" / "lib" / "lambdas" / "token-issuer"

HOST = "127.0.0.1"
DEFAULT_PORT = 8099
MAX_BODY_BYTES = 256 * 1024
# The sentinel the wrapper substitutes for a dev-bypass event's missing token. Only DevBypassVerifier
# (below, this file only) accepts it.
_DEV_BYPASS_PREFIX = "local-dev-bypass:"


def _paths() -> None:
    for p in (str(AGENT_SRC), str(TOKEN_ISSUER_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


class DevBypassVerifier:
    """DEV ONLY. Accepts the dev-bypass sentinel, a Dev Login token, or (if configured) a Cognito token."""

    def __init__(self, cognito=None):
        self._cognito = cognito
        self._dev_login = None
        try:
            import dev_login  # token-issuer/dev_login.py (DEV ONLY verifier for the UI's Dev Login token)

            self._dev_login = dev_login.DevLoginUserTokenVerifier()
        except Exception:  # noqa: BLE001 — Dev Login is optional
            self._dev_login = None

    def verify(self, token, now):
        if isinstance(token, str) and token.startswith(_DEV_BYPASS_PREFIX):
            sub = token[len(_DEV_BYPASS_PREFIX):]
            if sub:
                return {"sub": sub}
            raise ValueError("empty dev-bypass actor")
        if self._cognito is not None:
            try:
                return self._cognito.verify(token, now=now)
            except Exception:  # noqa: BLE001 — fall through to Dev Login
                pass
        if self._dev_login is not None:
            return self._dev_login.verify(token, now=now)
        raise ValueError("token not accepted")


def build_service(dev_bypass: bool):
    _paths()
    import boto3

    from ingestion.handlers.asset_service import AssetService
    from ingestion.registrar import allowlist_from_env
    from ingestion.store import AssetStore

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    table = os.environ.get("PROJECTS_TABLE", "")
    bucket = os.environ.get("INGEST_BUCKET") or os.environ.get("UPLOAD_BUCKET") or ""
    if not table or not bucket:
        sys.exit("PROJECTS_TABLE and INGEST_BUCKET (or UPLOAD_BUCKET) are required")
    store = AssetStore.open(table, endpoint_url=os.environ.get("PROJECTS_DDB_ENDPOINT") or None,
                            region_name=region)
    cognito = None
    if os.environ.get("USER_POOL_ID") and os.environ.get("USER_POOL_CLIENT_ID"):
        from cognito import CognitoUserTokenVerifier

        cognito = CognitoUserTokenVerifier(region=region, user_pool_id=os.environ["USER_POOL_ID"],
                                           client_id=os.environ["USER_POOL_CLIENT_ID"])
    if dev_bypass:
        verifier = DevBypassVerifier(cognito)
    elif cognito is not None:
        verifier = cognito
    else:
        sys.exit("USER_POOL_ID + USER_POOL_CLIENT_ID are required without --dev-bypass")
    service = AssetService(store=store, verifier=verifier, s3_client=boto3.client("s3", region_name=region),
                           ingest_bucket=bucket, allowlist=allowlist_from_env())
    print(f"[local-asset-service] table={table} bucket={bucket} region={region} "
          f"dev_bypass={'on' if dev_bypass else 'off'}", flush=True)
    return service


def prepare_event(event: object, dev_bypass: bool) -> object:
    """Map a dev-bypass event (no ``userToken``, a ``devBypassActor``) to the sentinel token; else as is."""
    if not isinstance(event, dict):
        return event
    event = dict(event)
    actor = event.pop("devBypassActor", None)
    if dev_bypass and not event.get("userToken") and isinstance(actor, str) and actor:
        event["userToken"] = _DEV_BYPASS_PREFIX + actor
    return event


def serve(service, port: int, dev_bypass: bool):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/invoke":
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
            try:
                reply = service.handle(prepare_event(event, dev_bypass))
            except Exception as exc:  # noqa: BLE001 — mirror a Lambda function error as a 500
                sys.stderr.write(f"[local-asset-service] handler error: {type(exc).__name__}: {exc}\n")
                return self._send(500, {"errorType": type(exc).__name__})
            self._send(200, reply)

        def log_message(self, fmt, *args):  # command + path + status only; never a body or token
            sys.stderr.write(f"[local-asset-service] {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    httpd = ThreadingHTTPServer((HOST, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="local-asset-service", daemon=True).start()
    print(f"[local-asset-service] http://{HOST}:{port}  POST /invoke", flush=True)
    print(f"[local-asset-service] proxy env: export LOCAL_ASSET_SERVICE_URL=http://{HOST}:{port}", flush=True)
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--dev-bypass", action="store_true",
                    help="DEV ONLY: accept the proxy's dev-bypass actor and Dev Login tokens")
    a = ap.parse_args()
    service = build_service(a.dev_bypass)
    httpd = serve(service, a.port, a.dev_bypass)
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    while not done.wait(0.5):  # a timed wait, so SIGINT/SIGTERM handlers get to run
        pass
    httpd.shutdown()
    httpd.server_close()


if __name__ == "__main__":
    main()
