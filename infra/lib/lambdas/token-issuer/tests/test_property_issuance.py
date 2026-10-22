"""Property test: first issuance iff access at the read (media-harness-projects-access task 3.6).

Feature: media-harness-projects-access, Property 6: Issuance iff access at the read

A Hypothesis ``RuleBasedStateMachine`` drives an in-memory Project registry through invite, remove, leave,
delete, tombstone, and issuance steps. ``issue_handler`` must return a token EXACTLY when the registry read
state grants access (META active, not tombstoned, and the Actor's MEMBER item present), and never after a
committed remove, leave, or delete for that (sub, project) pair (Req 4.8, 4.11, 1.23, 1.27, 2.8).
"""
from __future__ import annotations

from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

import index
import tokens

REGION = "us-west-2"
CURRENT_KID = "1234abcd-12ab-34cd-56ef-1234567890ab"
ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
TABLE = "mediaworks-harness-projects-test"

SUBS = ["s-owner", "s-alice", "s-bob"]
OWNER = SUBS[0]
PIDS = ["p" + "a" * 32, "p" + "b" * 32]
UNCREATED_PID = "p" + "c" * 32  # a well-formed id that is never created
MALFORMED_IDS = ["", "default", "P" + "a" * 32, "p" + "a" * 31, "../" + PIDS[0], None, 123]

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Kms:
    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        return {"Signature": _KEY.sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))}


class _EchoVerifier:
    def verify(self, token, *, now):
        return {"sub": token}


class _Registry:
    """The reference model: pid -> {status, tombstone, members:set}. ``access`` is the ground truth."""

    def __init__(self) -> None:
        self.projects: dict[str, dict] = {
            pid: {"status": "active", "tombstone": False, "members": {OWNER}} for pid in PIDS
        }

    def access(self, sub: str, pid: str) -> bool:
        p = self.projects.get(pid)
        return bool(p and p["status"] == "active" and not p["tombstone"] and sub in p["members"])


class _RegistryClient:
    """Reads the live model so the handler sees exactly the committed state."""

    def __init__(self, registry: _Registry) -> None:
        self.r = registry

    def batch_get_item(self, *, RequestItems):
        (name, spec), = RequestItems.items()
        assert spec["ConsistentRead"] is True
        items = []
        for key in spec["Keys"]:
            pid = key["PK"].split("PROJECT#", 1)[1]
            p = self.r.projects.get(pid)
            if p is None:
                continue
            if key["SK"] == "META":
                item = {"PK": key["PK"], "SK": "META", "status": p["status"]}
                if p["tombstone"]:
                    item["tombstone"] = True
                items.append(item)
            elif key["SK"].startswith("MEMBER#"):
                sub = key["SK"].split("MEMBER#", 1)[1]
                if sub in p["members"]:
                    items.append({"PK": key["PK"], "SK": key["SK"], "role": "member"})
        return {"Responses": {name: items}, "UnprocessedKeys": {}}


class IssuanceMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.registry = _Registry()
        table = SimpleNamespace(name=TABLE, meta=SimpleNamespace(client=_RegistryClient(self.registry)))
        self.handler = index.make_issue_handler(
            _EchoVerifier(), table, tokens.KmsSigner(_Kms(), CURRENT_KID), ISS, AUD, 3600,
            emit_metric=lambda *_: None)

    @rule(pid=st.sampled_from(PIDS), sub=st.sampled_from(SUBS))
    def invite(self, pid, sub):
        p = self.registry.projects.get(pid)
        if p and p["status"] == "active" and not p["tombstone"]:
            p["members"].add(sub)

    @rule(pid=st.sampled_from(PIDS), sub=st.sampled_from(SUBS[1:]))
    def remove(self, pid, sub):
        p = self.registry.projects.get(pid)
        if p:
            p["members"].discard(sub)

    @rule(pid=st.sampled_from(PIDS), sub=st.sampled_from(SUBS))
    def leave(self, pid, sub):
        p = self.registry.projects.get(pid)
        if p:
            p["members"].discard(sub)

    @rule(pid=st.sampled_from(PIDS))
    def delete(self, pid):
        p = self.registry.projects.get(pid)
        if p:
            p["status"] = "deleted"

    @rule(pid=st.sampled_from(PIDS))
    def tombstone(self, pid):
        p = self.registry.projects.get(pid)
        if p:
            p["tombstone"] = True

    @rule(sub=st.sampled_from(SUBS), pid=st.sampled_from(PIDS + [UNCREATED_PID]))
    def issue_matches_access(self, sub, pid):
        resp = self.handler({"userToken": sub, "projectId": pid})
        if self.registry.access(sub, pid):
            assert resp["status"] == "ok" and resp["token"]
        else:
            assert resp == {"status": "not_found"}

    @rule(sub=st.sampled_from(SUBS), bad=st.sampled_from(MALFORMED_IDS))
    def malformed_id_is_not_found(self, sub, bad):
        assert self.handler({"userToken": sub, "projectId": bad}) == {"status": "not_found"}

    @invariant()
    def issuance_always_tracks_access(self):
        for sub in SUBS:
            for pid in PIDS + [UNCREATED_PID]:
                resp = self.handler({"userToken": sub, "projectId": pid})
                granted = resp["status"] == "ok"
                assert granted == self.registry.access(sub, pid)


IssuanceMachine.TestCase.settings = settings(max_examples=100, deadline=None, stateful_step_count=30)
TestIssuanceMachine = IssuanceMachine.TestCase
