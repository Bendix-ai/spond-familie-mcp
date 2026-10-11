"""OAuth for én eier, slik at claude.ai (web, mobil, Desktop) kan koble til.

claude.ai registrerer seg selv (DCR), sender brukeren til ``/authorize`` med
PKCE, og fornyer tokenet selv. Vi er både autorisasjons- og ressursserver.

- **Hvem får registrere seg:** bare klienter med redirect til Claude
  (``claude.ai``/``claude.com`` sin ``/api/mcp/auth_callback``) eller loopback
  (Claude Code). Alt annet avvises ved registrering.
- **Hvem får tilgang:** den som kan passfrasen (scrypt-hash i ``owner.json``,
  laget av ``spond-familie owner-setup``). 5 feil på 15 min → 15 min sperre.
- **Hvor lenge:** access-token 1 t, refresh-token 90 d glidende og roterende.
  Tokenene lagres som sha256 i ``oauth.json`` (0600), så en omstart av tjenesten
  ikke tvinger frem ny tilkobling i claude.ai.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .tokens import _read_json, _write_private

SCOPE = "spond"
SCOPES = ["spond", "offline_access"]
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 86400
CODE_TTL_S = 300
PENDING_TTL_S = 600
MAX_PENDING = 50
MAX_CLIENTS = 20
FAIL_WINDOW_S = 15 * 60
FAIL_LIMIT = 5
LOCKOUT_S = 15 * 60

CLAUDE_CALLBACKS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
SCRYPT_MAXMEM = 128 * 1024 * 1024


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redirect_allowed(uri: str) -> bool:
    if uri in CLAUDE_CALLBACKS:
        return True
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


# --------------------------------------------------------------------------- #
# Eierens passfrase
# --------------------------------------------------------------------------- #
def hash_passphrase(passphrase: str, *, n: int = 2**15) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=8, p=1,
                            maxmem=SCRYPT_MAXMEM, dklen=32)
    return {"scrypt": {"n": n, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode(),
                       "hash": base64.b64encode(digest).decode()}}


def verify_passphrase(owner: dict, passphrase: str) -> bool:
    s = (owner or {}).get("scrypt") or {}
    try:
        salt = base64.b64decode(s["salt"])
        expected = base64.b64decode(s["hash"])
        got = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=int(s["n"]), r=int(s["r"]),
                             p=int(s["p"]), maxmem=SCRYPT_MAXMEM, dklen=len(expected))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class OwnerRefreshToken(RefreshToken):
    grant: str


class OwnerAccessToken(AccessToken):
    grant: str


class OwnerOAuthProvider:
    def __init__(self, state_path: Path, owner_path: Path, public_base: str, resource_url: str,
                 *, clock=time.time) -> None:
        self.state_path = Path(state_path)
        self.owner_path = Path(owner_path)
        self.base = public_base.rstrip("/")
        self.resource = resource_url.rstrip("/")
        self._now = clock
        self._lock = asyncio.Lock()
        self._pending: dict[str, dict] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        data = _read_json(self.state_path) or {}
        self._clients: dict[str, dict] = data.get("clients", {})
        self._client_used: dict[str, float] = data.get("client_used", {})
        self._access: dict[str, dict] = data.get("access", {})
        self._refresh: dict[str, dict] = data.get("refresh", {})
        self._failures: list[float] = data.get("failures", [])
        self._locked_until: float = data.get("locked_until", 0.0)

    # -- lagring ------------------------------------------------------------
    def _save(self) -> None:
        now = self._now()
        self._access = {k: v for k, v in self._access.items() if v["expires_at"] > now}
        self._refresh = {k: v for k, v in self._refresh.items() if v["expires_at"] > now}
        _write_private(self.state_path, {
            "clients": self._clients, "client_used": self._client_used,
            "access": self._access, "refresh": self._refresh,
            "failures": self._failures, "locked_until": self._locked_until,
        })

    # -- klienter -----------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not redirect_allowed(u)]
        if not uris or bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "Denne serveren godtar bare Claude (claude.ai/claude.com) og loopback som klient.",
            )
        client_info.scope = " ".join(SCOPES)
        async with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                oldest = min(self._clients, key=lambda c: self._client_used.get(c, 0))
                self._clients.pop(oldest, None)
                self._client_used.pop(oldest, None)
            self._clients[client_info.client_id] = client_info.model_dump(mode="json")
            self._client_used[client_info.client_id] = self._now()
            self._save()

    # -- autorisering -------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_request", "Ukjent resource for denne serveren.")
        now = self._now()
        self._pending = {k: v for k, v in self._pending.items() if v["expires"] > now}
        if len(self._pending) >= MAX_PENDING:
            raise AuthorizeError("temporarily_unavailable", "For mange ventende innlogginger.")
        req = secrets.token_urlsafe(32)
        self._pending[req] = {"client_id": client.client_id, "params": params, "expires": now + PENDING_TTL_S}
        return f"{self.base}/owner/login?req={req}"

    def pending(self, req: str) -> dict | None:
        p = self._pending.get(req or "")
        return p if p and p["expires"] > self._now() else None

    def lockout_left(self) -> int:
        return max(0, int(self._locked_until - self._now()))

    async def check_passphrase(self, req: str, passphrase: str) -> str | None:
        """Riktig passfrase → redirect-URL med kode. Feil → None (og telles)."""
        pend = self.pending(req)
        if pend is None or self.lockout_left():
            return None
        owner = _read_json(self.owner_path)
        ok = bool(owner) and await asyncio.to_thread(verify_passphrase, owner, passphrase)
        async with self._lock:
            now = self._now()
            if not ok:
                self._failures = [t for t in self._failures if t > now - FAIL_WINDOW_S] + [now]
                if len(self._failures) >= FAIL_LIMIT:
                    self._locked_until = now + LOCKOUT_S
                    self._failures = []
                self._save()
                return None
            self._failures = []
            self._save()
        self._pending.pop(req, None)
        params: AuthorizationParams = pend["params"]
        client = await self.get_client(pend["client_id"])
        scopes = sorted(set(params.scopes or (client.scope or SCOPE).split()) | {SCOPE})
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code, scopes=scopes, expires_at=self._now() + CODE_TTL_S,
            client_id=pend["client_id"], code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource, subject="owner",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    # -- tokener ------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str], grant: str) -> OAuthToken:
        now = self._now()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        rec = {"client_id": client_id, "scopes": scopes, "resource": self.resource, "grant": grant}
        self._access[_sha(access)] = {**rec, "expires_at": int(now + ACCESS_TTL_S)}
        self._refresh[_sha(refresh)] = {**rec, "expires_at": int(now + REFRESH_TTL_S)}
        self._client_used[client_id] = now
        self._save()
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL_S, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Koden er allerede brukt.")
        async with self._lock:
            return self._issue(client.client_id, authorization_code.scopes, secrets.token_hex(8))

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> OwnerRefreshToken | None:
        rec = self._refresh.get(_sha(refresh_token))
        if rec is None or rec["client_id"] != client.client_id or rec["expires_at"] < self._now():
            return None
        return OwnerRefreshToken(token=refresh_token, client_id=rec["client_id"], scopes=rec["scopes"],
                                 expires_at=rec["expires_at"], resource=rec["resource"],
                                 subject="owner", grant=rec["grant"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: OwnerRefreshToken, scopes: list[str]) -> OAuthToken:
        async with self._lock:
            if self._refresh.pop(_sha(refresh_token.token), None) is None:
                raise TokenError("invalid_grant", "Refresh-tokenet er allerede brukt.")
            self._drop_grant(refresh_token.grant, access_only=True)
            scopes = sorted(set(scopes) | {SCOPE})
            return self._issue(client.client_id, scopes, refresh_token.grant)

    async def load_access_token(self, token: str) -> OwnerAccessToken | None:
        rec = self._access.get(_sha(token))
        if rec is None or rec["expires_at"] < self._now():
            return None
        return OwnerAccessToken(token=token, client_id=rec["client_id"], scopes=rec["scopes"],
                                expires_at=rec["expires_at"], resource=rec["resource"],
                                subject="owner", grant=rec["grant"])

    def _drop_grant(self, grant: str, *, access_only: bool = False) -> None:
        self._access = {k: v for k, v in self._access.items() if v["grant"] != grant}
        if not access_only:
            self._refresh = {k: v for k, v in self._refresh.items() if v["grant"] != grant}

    async def revoke_token(self, token) -> None:
        grant = getattr(token, "grant", None)
        if grant is None:
            return
        async with self._lock:
            self._drop_grant(grant)
            self._save()

    async def revoke_all(self) -> None:
        async with self._lock:
            self._access, self._refresh = {}, {}
            self._save()


# --------------------------------------------------------------------------- #
# Innloggingssiden
# --------------------------------------------------------------------------- #
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self' https://claude.ai https://claude.com http://localhost:* http://127.0.0.1:*")
# NB: ikke «no-referrer» her: da sender nettleseren «Origin: null» på skjemaet,
# og opprinnelsessjekken avviser innloggingen (skjedde 11.10.2026). «same-origin»
# sender fortsatt ingen referrer til andre nettsteder (heller ikke til claude.ai).
_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff"}


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Spond-MCP</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 16px;color:#1d1d1f;background:#fafafa}}
h1{{font-size:1.3rem}}input{{width:100%;box-sizing:border-box;font-size:1rem;padding:.6rem;margin:.4rem 0 1rem;border:1px solid #bbb;border-radius:8px}}
button{{font-size:1rem;padding:.6rem 1.2rem;border:0;border-radius:8px;background:#1d1d1f;color:#fff}}
.muted{{color:#666;font-size:.9rem}}.err{{color:#b00020}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}input{{background:#222;color:#eee;border-color:#444}}button{{background:#eee;color:#111}}.muted{{color:#aaa}}}}</style>
</head><body>{body}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _form(provider: OwnerOAuthProvider, req: str, pend: dict, error: str | None = None) -> str:
    client = provider._clients.get(pend["client_id"], {})
    name = html.escape(client.get("client_name") or "Claude")
    host = html.escape(urlparse(str(pend["params"].redirect_uri)).netloc)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<h1>Gi {name} tilgang til Spond</h1>
<p class="muted">Etter innlogging sendes du tilbake til <b>{host}</b>. Tilgangen gjelder lesing,
svar for barna og trenerverktøy, med bekreftelse før alt som endrer noe.</p>
<p class="err"><b>Fortsett bare hvis du selv nettopp trykket «Connect» i Claude.</b>
Har du fått denne lenken fra noen andre, lukk siden.</p>{err}
<form method="post" action="/owner/login"><input type="hidden" name="req" value="{html.escape(req)}">
<label for="p">Passfrase</label><input id="p" name="passphrase" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Gi tilgang</button></form>"""


def register_login_routes(mcp, provider: OwnerOAuthProvider) -> None:
    @mcp.custom_route("/owner/login", methods=["GET", "POST"])
    async def owner_login(request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            pend = provider.pending(req)
            if pend is None:
                return _page("<h1>Lenken er utløpt</h1><p>Start tilkoblingen på nytt fra Claude.</p>", 400)
            return _page(_form(provider, req, pend))

        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != provider.base:
            return _page("<h1>Avvist</h1><p>Skjemaet kom fra en annen side enn denne serveren "
                         f"(Origin: {html.escape(origin)}). Passfrasen ble ikke sjekket.</p>", 403)
        form = await request.form()
        req = str(form.get("req", ""))
        pend = provider.pending(req)
        if pend is None:
            return _page("<h1>Lenken er utløpt</h1><p>Start tilkoblingen på nytt fra Claude.</p>", 400)
        if provider.lockout_left():
            mins = provider.lockout_left() // 60 + 1
            return _page(f"<h1>For mange forsøk</h1><p>Prøv igjen om {mins} min.</p>", 429)
        target = await provider.check_passphrase(req, str(form.get("passphrase", "")))
        if target is None:
            if provider.lockout_left():
                return _page("<h1>For mange forsøk</h1><p>Prøv igjen om 15 min.</p>", 429)
            return _page(_form(provider, req, pend, "Feil passfrase."), 401)
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return Response(json.dumps({"ok": True}), media_type="application/json")
