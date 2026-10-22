"""Property test for Token_Refresh (media-harness-projects-access task 3.7).

Feature: media-harness-projects-access, Property 17: Token_Refresh issues iff the presented token is valid and membership holds

Presented tokens are drawn from: issuer-signed tokens with arbitrary ``exp`` (unexpired, expired-within-
grace, and beyond grace), a foreign-key signer, a retired key still in ``PUBLISHED_KEY_IDS``, other
``iss``/``aud``, no or malformed ``project_id``, Cognito-shaped and machine-token-shaped tokens, truncated
and non-JWT strings, and absent. Each request also carries extra keys naming OTHER Actors/Projects (which
must be ignored), and membership for the token's own pair is interleaved on/off.

Asserted (Req 4.11, 4.21, 4.22, 4.23, 4.28):
  * ``ok`` exactly when the token verifies AND membership holds; same ``sub``/``project_id`` and a NEW
    ``jti`` on every issued token;
  * ``invalid_token`` vs ``access_ended`` selected correctly, and NOTHING signed on refusal;
  * both sides of the 8 h boundary: ``now - exp == REFRESH_GRACE_S`` is accepted (then membership-checked),
    ``REFRESH_GRACE_S + 1`` is ``invalid_token``; unexpired and expired-within-grace are treated alike.
"""
from __future__ import annotations

import hashlib

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from hypothesis import given, settings
from hypothesis import strategies as st
from types import SimpleNamespace

import tokens
from index import make_refresh_handler
from tokens import REFRESH_GRACE_S

ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
TABLE = "mediaworks-harness-projects-test"
IAT = 1_800_000_000

CURRENT_KID = "11111111-1111-1111-1111-111111111111"
RETIRED_KID = "22222222-2222-2222-2222-222222222222"  # still published
UNPUBLISHED_KID = "33333333-3333-3333-3333-333333333333"

_KEYS = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)
         for kid in (CURRENT_KID, RETIRED_KID, UNPUBLISHED_KID)}
_FOREIGN = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBS = {CURRENT_KID: _KEYS[CURRENT_KID].public_key(), RETIRED_KID: _KEYS[RETIRED_KID].public_key()}


class LocalRsaVerifier:
    """Test-only ``tokens.Verifier`` over local RSA public keys (the two published keys here)."""

    def __init__(self, pubkeys):
        self._pub = dict(pubkeys)

    def __contains__(self, kid):
        return isinstance(kid, str) and kid in self._pub

    def verify_digest(self, kid, digest, signature):
        try:
            self._pub[kid].verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False


_VERIFIER = LocalRsaVerifier(_PUBS)

SUBS = ["s-alice", "s-bob"]
PIDS = ["p" + "a" * 32, "p" + "b" * 32]
# A fixed decoy pair, distinct from any token pair, that the extra request keys name.
DECOY_SUB = "s-decoy"
DECOY_PID = "p" + "d" * 32


class _MintKms:
    """Signs presented tokens with the given kid's key; isolated from the handler's own signer."""

    def __init__(self, kid: str) -> None:
        self._key = _KEYS[kid]

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        return {"Signature": self._key.sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))}


class _HandlerKms:
    """The handler's signer: current key only, with a sign-call log to prove nothing is signed on refusal."""

    def __init__(self) -> None:
        self.sign_calls: list[str] = []

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        self.sign_calls.append(KeyId)
        return {"Signature": _KEYS[CURRENT_KID].sign(Message, padding.PKCS1v15(),
                                                     utils.Prehashed(hashes.SHA256()))}


class _MembershipClient:
    """Grants exactly the (pid, sub) pairs in ``granted``; records whether it was read at all."""

    def __init__(self, granted: set[tuple[str, str]]) -> None:
        self.granted = granted
        self.reads = 0

    def batch_get_item(self, *, RequestItems):
        self.reads += 1
        (name, spec), = RequestItems.items()
        items = []
        for key in spec["Keys"]:
            pid = key["PK"].split("PROJECT#", 1)[1]
            if key["SK"] == "META" and any(p == pid for (p, _s) in self.granted):
                items.append({"PK": key["PK"], "SK": "META", "status": "active"})
            elif key["SK"].startswith("MEMBER#"):
                sub = key["SK"].split("MEMBER#", 1)[1]
                if (pid, sub) in self.granted:
                    items.append({"PK": key["PK"], "SK": key["SK"], "role": "member"})
        return {"Responses": {name: items}, "UnprocessedKeys": {}}


def _mint(kid, *, sub, pid, iss=ISS, aud=AUD, iat=IAT, lifetime=600):
    return tokens.issue(tokens.KmsSigner(_MintKms(kid), kid), iss=iss, aud=aud, sub=sub,
                        project_id=pid, now=iat, lifetime=lifetime)


def _foreign_signed(sub, pid):
    signing_input = _mint(CURRENT_KID, sub=sub, pid=pid).token.rsplit(".", 1)[0]
    sig = _FOREIGN.sign(hashlib.sha256(signing_input.encode()).digest(), padding.PKCS1v15(),
                        utils.Prehashed(hashes.SHA256()))
    return f"{signing_input}.{tokens._b64url(sig)}"


def _hand_encoded(claims):
    """A token signed by the current key but with an arbitrary (possibly incomplete) claim set."""
    return tokens.encode(claims, tokens.KmsSigner(_MintKms(CURRENT_KID), CURRENT_KID))


# --------------------------------------------------------------------------------------------------
# A case = (token string | None, is_valid, sub, pid, now, prev_jti | None).
# is_valid == "verify would succeed" (independent of membership).
# --------------------------------------------------------------------------------------------------


@st.composite
def valid_cases(draw, kid):
    sub = draw(st.sampled_from(SUBS))
    pid = draw(st.sampled_from(PIDS))
    lifetime = draw(st.integers(min_value=60, max_value=3600))
    # Cover both sides of the grace boundary explicitly, plus the general range (iat <= now ⟺ age >= -lifetime).
    age = draw(st.one_of(
        st.integers(min_value=-lifetime, max_value=REFRESH_GRACE_S + 5),
        st.sampled_from([-lifetime, -1, 0, 1, REFRESH_GRACE_S - 1, REFRESH_GRACE_S, REFRESH_GRACE_S + 1]),
    ))
    age = max(age, -lifetime)
    minted = _mint(kid, sub=sub, pid=pid, lifetime=lifetime)
    now = float(minted.exp + age)
    return (minted.token, age <= REFRESH_GRACE_S, sub, pid, now, minted.jti)


@st.composite
def invalid_cases(draw):
    sub = draw(st.sampled_from(SUBS))
    pid = draw(st.sampled_from(PIDS))
    now = float(IAT + 100)
    kind = draw(st.sampled_from([
        "unpublished_kid", "foreign", "other_iss", "other_aud",
        "missing_pid", "bad_pid", "cognito", "machine", "truncated", "garbage", "dots", "none",
    ]))
    if kind == "unpublished_kid":
        token = _mint(UNPUBLISHED_KID, sub=sub, pid=pid).token
    elif kind == "foreign":
        token = _foreign_signed(sub, pid)
    elif kind == "other_iss":
        token = _mint(CURRENT_KID, sub=sub, pid=pid, iss="https://other.example").token
    elif kind == "other_aud":
        token = _mint(CURRENT_KID, sub=sub, pid=pid, aud="mediaworks-harness-search-prod").token
    elif kind == "missing_pid":
        token = _hand_encoded({"iss": ISS, "aud": AUD, "sub": sub, "iat": IAT, "exp": IAT + 600,
                               "jti": "x"})
    elif kind == "bad_pid":
        token = _hand_encoded({"iss": ISS, "aud": AUD, "sub": sub, "project_id": "not-a-pid",
                               "iat": IAT, "exp": IAT + 600, "jti": "x"})
    elif kind == "cognito":
        token = jwt.encode({"sub": sub, "iss": "https://cognito-idp.us-west-2.amazonaws.com/pool",
                            "client_id": "c", "token_use": "access", "iat": IAT, "exp": IAT + 3600},
                           _FOREIGN, algorithm="RS256", headers={"kid": "pool-kid-1"})
    elif kind == "machine":
        token = jwt.encode({"sub": sub, "client_id": "machine", "scope": "x", "iat": IAT, "exp": IAT + 3600},
                           _FOREIGN, algorithm="RS256", headers={"kid": "machine-kid"})
    elif kind == "truncated":
        token = _mint(CURRENT_KID, sub=sub, pid=pid).token[:-10]
    elif kind == "garbage":
        token = "not-a-jwt"
    elif kind == "dots":
        token = "a.b.c"
    else:
        token = None
    return (token, False, sub, pid, now, None)


@settings(max_examples=400, deadline=None)
@given(
    case=st.one_of(valid_cases(CURRENT_KID), valid_cases(RETIRED_KID), invalid_cases()),
    membership=st.booleans(),
)
def test_refresh_issues_iff_valid_and_member(case, membership):
    token, is_valid, sub, pid, now, prev_jti = case

    # Membership for the token's own pair = `membership`; the DECOY pair (named only in the extra request
    # keys) is granted the OPPOSITE, so if the handler ever used an event key instead of the verified
    # claims, the outcome would flip and this test would catch it.
    granted: set[tuple[str, str]] = set()
    if membership:
        granted.add((pid, sub))
    else:
        granted.add((DECOY_PID, DECOY_SUB))

    client = _MembershipClient(granted)
    kms = _HandlerKms()
    handler = make_refresh_handler(
        _VERIFIER, SimpleNamespace(name=TABLE, meta=SimpleNamespace(client=client)),
        tokens.KmsSigner(kms, CURRENT_KID), ISS, AUD, 600,
        clock=lambda: now, emit_metric=lambda *_: None)

    event = {"token": token, "sub": DECOY_SUB, "actorId": DECOY_SUB,
             "projectId": DECOY_PID, "project_id": DECOY_PID}
    resp = handler(event)

    if not is_valid:
        assert resp == {"status": "invalid_token"}
        assert kms.sign_calls == [] and client.reads == 0
    elif membership:
        assert set(resp) == {"status", "token", "exp", "jti"} and resp["status"] == "ok"
        assert kms.sign_calls == [CURRENT_KID]
        claims = tokens.verify(resp["token"], _VERIFIER, ISS, AUD, now)
        assert claims["sub"] == sub and claims["project_id"] == pid
        assert resp["jti"] != prev_jti  # a new jti on every issued token
        assert jwt.get_unverified_header(resp["token"])["kid"] == CURRENT_KID
    else:
        assert resp == {"status": "access_ended"}
        assert kms.sign_calls == []
