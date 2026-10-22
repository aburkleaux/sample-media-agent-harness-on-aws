"""``scripts/local_asset_service.py``: the dev-bypass actor is honoured ONLY with ``--dev-bypass``."""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
_spec = importlib.util.spec_from_file_location(
    "local_asset_service", os.path.join(HERE, "..", "scripts", "local_asset_service.py")
)
las = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(las)


def test_dev_bypass_actor_ignored_without_the_flag():
    ev = las.prepare_event({"projectId": "p", "devBypassActor": "local-dev-user"}, dev_bypass=False)
    assert "devBypassActor" not in ev and not ev.get("userToken")


def test_dev_bypass_actor_becomes_the_sentinel_with_the_flag():
    ev = las.prepare_event({"projectId": "p", "devBypassActor": "local-dev-user"}, dev_bypass=True)
    assert "devBypassActor" not in ev
    assert las.DevBypassVerifier().verify(ev["userToken"], now=0) == {"sub": "local-dev-user"}


def test_a_real_token_is_never_replaced():
    ev = las.prepare_event({"userToken": "abc", "devBypassActor": "someone-else"}, dev_bypass=True)
    assert ev["userToken"] == "abc"


def test_the_lambda_handler_never_accepts_the_sentinel():
    # The deployed handler's verifier is Cognito; the sentinel is not a JWT and is refused as 401.
    from ingestion.handlers.asset_service import AssetService

    class Cognitoish:
        def verify(self, token, now):
            raise ValueError("not a JWT")

    svc = AssetService(store=type("S", (), {"table": None})(), verifier=Cognitoish(), s3_client=None,
                       ingest_bucket="b")
    reply = svc.handle({"userToken": "local-dev-bypass:x", "projectId": "p" + "a" * 32,
                        "command": "capabilities", "args": {}})
    assert reply["status"] == 401
