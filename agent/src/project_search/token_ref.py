"""ProjectTokenRef: the only holder of a turn's Project_Token (design Component 7, D16, Req 4.20–4.26).

`await ref.current()` is the only way code obtains the raw token for a request. It refreshes the token
through Token_Refresh, in runtime code and silently, when it is inside the Refresh_Threshold
(`min(300, lifetime / 2)`) or expired within the Refresh_Grace (8 h, the runtimes' maxLifetime).
The token is parsed WITHOUT verification only to read its timing and identity claims; the gateway and
Token_Refresh do the verifying.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SKEW_S = 5
REFRESH_GRACE_S = 28800  # requirements decision 16


class AccessEnded(Exception):
    """Token_Refresh refused: the Actor is no longer the Owner or a Member, or the Project is deleted."""


class NotEstablished(Exception):
    """No usable token: refresh failed, the token is past the grace, or it could not be parsed."""


class RefreshOutcome:
    __slots__ = ("kind", "token", "reason")

    def __init__(self, kind: str, token: Optional[str] = None, reason: str = ""):
        self.kind, self.token, self.reason = kind, token, reason  # kind: ok | access_ended | failed

    @classmethod
    def ok(cls, token: str) -> "RefreshOutcome":
        return cls("ok", token=token)

    @classmethod
    def access_ended(cls) -> "RefreshOutcome":
        return cls("access_ended")

    @classmethod
    def failed(cls, reason: str) -> "RefreshOutcome":
        return cls("failed", reason=reason)

    def __repr__(self) -> str:  # never prints a token
        return f"<refresh-outcome {self.kind}{' ' + self.reason if self.reason else ''}>"


Refresher = Callable[[str], Awaitable[RefreshOutcome]]


def parse_claims(raw: str) -> dict:
    """Decode the JWT payload without verifying it. Raises ValueError on a malformed token."""
    try:
        seg = raw.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("malformed project token") from exc
    if not isinstance(claims, dict) or not all(isinstance(claims.get(k), int) for k in ("iat", "exp")):
        raise ValueError("project token lacks integer iat/exp")
    return claims


class ProjectTokenRef:
    def __init__(self, raw: str, refresher: Optional[Refresher] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        claims = parse_claims(raw)
        self.__raw = raw
        self._claims = claims
        self.state = "live"
        self._refresher = refresher
        self._clock, self._sleep = clock, sleep
        self._lock = asyncio.Lock()

    # Identity and timing (safe to log except where noted).
    @property
    def jti(self) -> str:
        return str(self._claims.get("jti", ""))

    @property
    def project_id(self) -> str:
        return str(self._claims.get("project_id", ""))

    @property
    def sub(self) -> str:
        return str(self._claims.get("sub", ""))

    @property
    def exp(self) -> int:
        return self._claims["exp"]

    @property
    def iat(self) -> int:
        return self._claims["iat"]

    def mark_ended(self) -> None:
        self.state = "ended"

    def __repr__(self) -> str:
        return f"<project-token jti={self.jti}>"

    __str__ = __repr__

    async def current(self) -> str:
        async with self._lock:  # single flight: concurrent calls share one refresh
            if self.state == "ended":
                raise AccessEnded()
            now = self._clock()
            left = self.exp - now
            threshold = min(300, (self.exp - self.iat) / 2)
            if left >= threshold:
                return self.__raw
            if now - self.exp > REFRESH_GRACE_S:
                raise NotEstablished()
            if self._refresher is None:
                if left > SKEW_S:
                    return self.__raw
                raise NotEstablished()
            for attempt in range(2):
                outcome = await self._refresher(self.__raw)
                if outcome.kind == "ok" and outcome.token:
                    if self._swap(outcome.token):
                        return self.__raw
                    outcome = RefreshOutcome.failed("identity_mismatch")
                elif outcome.kind == "access_ended":
                    self.state = "ended"
                    logger.info("project_token_refreshed prev_jti=%s outcome=access_ended", self.jti)
                    raise AccessEnded()
                logger.info("project_token_refreshed prev_jti=%s outcome=failed reason=%s", self.jti, outcome.reason)
                if attempt == 0:
                    await self._sleep(1)
            if self.exp - self._clock() > SKEW_S:
                return self.__raw  # issuer outage: keep using a still-valid token (Req 4.25)
            raise NotEstablished()

    def _swap(self, new_raw: str) -> bool:
        try:
            claims = parse_claims(new_raw)
        except ValueError:
            return False
        if claims.get("sub") != self.sub or claims.get("project_id") != self.project_id:
            return False  # a refresh may never move the ref to another Actor or Project (Req 4.22)
        prev = self.jti
        self.__raw, self._claims = new_raw, claims
        logger.info("project_token_refreshed prev_jti=%s jti=%s outcome=ok", prev, self.jti)
        return True
