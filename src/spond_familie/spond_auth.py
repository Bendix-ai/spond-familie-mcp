"""Spond-innlogging og -fornying, uten biblioteket.

``spond==1.2.1`` bruker bare access-tokenet (24 t) og logger inn med passord på
nytt hver gang. Spond sin egen web-klient fornyer i stedet med refresh-tokenet
(90 d). Målt 11.10.2026 (``scripts/probe_spond_refresh.py``):

- ``POST auth2/login/refresh {"value": <refresh>}`` uten bearer → 200 med nytt
  access- OG refresh-token; refresh-utløpet glir 90 d fram for hver fornying.
- Et brukt refresh-token gir 401 med én gang (ingen nådeperiode), men dreper
  ikke resten av familien. Ny passordinnlogging (digesten) påvirker ikke andre.
- Gamle access-tokener virker til de utløper.
"""

from __future__ import annotations

import time
from datetime import datetime

import aiohttp

from .tokens import SpondTokens

API_BASE = "https://api.spond.com/core/v1/"
AUTH_TIMEOUT = aiohttp.ClientTimeout(total=30)
DEFAULT_ACCESS_S = 24 * 3600
DEFAULT_REFRESH_S = 90 * 86400


class SpondAuthError(RuntimeError):
    pass


class RefreshRejected(SpondAuthError):
    """Spond avviste refresh-tokenet (brukt, utløpt eller tilbakekalt)."""


class LoginFailed(SpondAuthError):
    """Feil brukernavn/passord eller annen avvisning ved innlogging."""


class AccountLocked(LoginFailed):
    """For mange mislykkede innlogginger; passordet må nullstilles i Spond."""


class TwoFactorRequired(SpondAuthError):
    def __init__(self, token: str, phone: str | None) -> None:
        super().__init__("Spond krever SMS-kode (2FA)")
        self.token = token
        self.phone = phone


def _epoch(value, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def parse_tokens(body, *, now: float, source: str) -> SpondTokens:
    if not isinstance(body, dict):
        raise LoginFailed("Uventet svar fra Spond")
    access = body.get("accessToken")
    if not isinstance(access, dict) or not access.get("token"):
        if body.get("token") and "phoneNumber" in body:
            raise TwoFactorRequired(body["token"], body.get("phoneNumber"))
        if "outOfLoginAttempts" in str(body):
            raise AccountLocked("Spond: for mange mislykkede innlogginger (nullstill passordet i appen)")
        raise LoginFailed(f"Spond avviste innloggingen ({sorted(body)[:4]})")
    refresh = body.get("refreshToken") or {}
    if not refresh.get("token"):
        raise LoginFailed("Spond-svaret mangler refreshToken")
    return SpondTokens(
        access=access["token"],
        access_exp=_epoch(access.get("expiration"), now + DEFAULT_ACCESS_S),
        refresh=refresh["token"],
        refresh_exp=_epoch(refresh.get("expiration"), now + DEFAULT_REFRESH_S),
        obtained_at=now,
        source=source,
    )


class SpondAuthApi:
    """Rå auth-kall. Injiseres i ``SpondSession`` så testene kan bytte den ut."""

    def __init__(self, api_base: str = API_BASE, wallclock=time.time) -> None:
        self.api = api_base
        self._now = wallclock

    async def _post(self, http: aiohttp.ClientSession, path: str, body: dict):
        async with http.post(self.api + path, json=body, timeout=AUTH_TIMEOUT,
                             headers={"content-type": "application/json"}) as r:
            try:
                data = await r.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                data = None
            return r.status, data

    async def login(self, http, email: str, password: str) -> SpondTokens:
        status, data = await self._post(http, "auth2/login", {"email": email, "password": password})
        if status >= 500:
            raise SpondAuthError(f"Spond-innlogging: HTTP {status}")
        if status != 200 and "outOfLoginAttempts" in str(data):
            raise AccountLocked("Spond: for mange mislykkede innlogginger (nullstill passordet i appen)")
        if status != 200:
            raise LoginFailed(f"Spond-innlogging: HTTP {status}")
        return parse_tokens(data, now=self._now(), source="login")

    async def refresh(self, http, refresh_token: str) -> SpondTokens:
        status, data = await self._post(http, "auth2/login/refresh", {"value": refresh_token})
        if status in (400, 401, 403):
            raise RefreshRejected(f"Spond avviste refresh-tokenet (HTTP {status})")
        if status != 200:
            raise SpondAuthError(f"Spond-fornying: HTTP {status}")
        return parse_tokens(data, now=self._now(), source="refresh")

    async def verify_2fa(self, http, token: str, code: str, phone: str | None) -> SpondTokens:
        status, data = await self._post(http, "auth2/2fa/verify",
                                        {"token": token, "code": code, "phoneNumber": phone})
        if status != 200:
            raise LoginFailed(f"SMS-koden ble avvist (HTTP {status})")
        return parse_tokens(data, now=self._now(), source="2fa")
