"""Langlivet, selvreparerende Spond-økt for MCP-serveren.

``SpondClient`` er laget for kortlivede kjøringer (én digest, så exit). En
MCP-server lever i timer, og da slår svakhetene i ``spond==1.2.1`` inn:

- biblioteket logger inn med passord hver gang tokenet mangler, og fornyer aldri;
- mislykket innlogging lukker aiohttp-økta for godt;
- ``get_groups``/``get_messages``/``change_response`` sjekker ikke status;
- ``SpondClient._call`` prøver på nytt ved tidsavbrudd — dobbel melding ved sending.

``SpondSession`` arver ``SpondClient`` (så digesten er urørt). Tokenet hentes fra
det delte ``TokenStore`` og fornyes med Spond sitt refresh-token (roterer, 90 d
glidende). Passord brukes bare som reserve, og bare når ``allow_password`` er på
(bare når SPOND_PASSWORD er satt). Vanligvis brukes passordet bare én gang, av
``spond-familie login``.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp

from ._base import CALL_SLEEP_SECONDS, Settings, SpondClient

from .spond_auth import (
    AccountLocked,
    LoginFailed,
    RefreshRejected,
    SpondAuthApi,
    TwoFactorRequired,
)
from .tokens import SpondTokens, TokenStore

REFRESH_MARGIN_S = 3600          # forny når access-tokenet har under 1 t igjen
LOGIN_COOLDOWN_S = 10 * 60       # etter avvist passordinnlogging
XLSX_MAGIC = b"PK\x03\x04"
SEED_HINT = "Spond-innloggingen må fornyes. Kjør i terminalen: spond-familie login"


class SpondError(RuntimeError):
    """Spond svarte med feil (ikke-2xx) eller noe uventet."""


class AuthExpired(SpondError):
    """401 fra Spond: tokenet er utløpt eller ugyldiggjort."""


class LoginBlocked(SpondError):
    """Passordinnlogging feilet nylig; vi venter før neste forsøk."""


class AuthDead(SpondError):
    """Ingen gyldig token, og passord er ikke tillatt (eller krever 2FA)."""


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, AuthExpired):
        return True
    text = str(exc)
    return "status 401" in text or "HTTP 401" in text


class SpondSession(SpondClient):
    """``SpondClient`` som tåler lange liv. Bruk ``ensure()`` før kall."""

    def __init__(self, config: Settings, *, store: TokenStore | None = None,
                 allow_password: bool = True, auth_api: SpondAuthApi | None = None,
                 wallclock=time.time) -> None:
        super().__init__(config)
        self._store = store if store is not None else TokenStore()
        self._allow_password = allow_password and bool(config.spond_password)
        self._wall = wallclock
        self._auth_api = auth_api or SpondAuthApi(wallclock=wallclock)
        self._tokens: SpondTokens | None = None
        self.logins = 0
        self.refreshes = 0

    # ------------------------------------------------------------------ #
    # Livssyklus
    # ------------------------------------------------------------------ #
    async def open(self) -> None:
        """Lag (eller gjenskap) den underliggende økta. Ingen nettverkskall."""
        s = self._spond
        if s is None or s.clientsession is None or s.clientsession.closed:
            await self.close()
            await self.__aenter__()
            s = self._spond
            # Biblioteket kaller login() (passord!) når token mangler. Rut via lageret.
            s.login = self._guarded_login
            if self._tokens is not None:
                s.token = self._tokens.access

    async def _guarded_login(self) -> None:
        await self._ensure_token(stale=self._client.token)

    async def close(self) -> None:
        await self.__aexit__(None, None, None)

    async def ensure(self) -> None:
        """Sørg for åpen økt og et access-token med minst 1 t igjen."""
        await self.open()
        await self._ensure_token()

    async def relogin(self) -> None:
        await self._ensure_token(stale=self._client.token)

    def _adopt(self, tokens: SpondTokens) -> None:
        self._tokens = tokens
        s = self._client
        if s.token != tokens.access:
            s.token = tokens.access
            s._auth = None
            s._chat_url = None

    def _usable(self, t: SpondTokens | None, stale: str | None, now: float) -> bool:
        return t is not None and t.access != stale and t.access_exp - now > REFRESH_MARGIN_S

    async def _ensure_token(self, stale: str | None = None) -> None:
        """Gyldig access-token: minne → disk → refresh → (passord) → AuthDead.

        ``stale`` er tokenet som nettopp fikk 401; det skal ikke brukes igjen.
        Alt etter minnet skjer under fillåsen, med ny lesing av disken, så to
        prosesser aldri bruker samme refresh-token (det er engangs hos Spond).
        """
        now = self._wall()
        if self._usable(self._tokens, stale, now):
            self._adopt(self._tokens)
            return
        async with self._store.locked():
            now = self._wall()
            disk = self._store.read()
            if self._usable(disk, stale, now):
                self._adopt(disk)
                return
            fresh: SpondTokens | None = None
            if disk is not None and disk.refresh_exp > now:
                try:
                    fresh = await self._auth_api.refresh(self._client.clientsession, disk.refresh)
                    self.refreshes += 1
                except RefreshRejected:
                    fresh = None
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    # Nettverksfeil: bruk det vi har så lenge det ikke er utløpt.
                    if disk.access != stale and disk.access_exp > now:
                        self._adopt(disk)
                        return
                    raise
            if fresh is None:
                fresh = await self._password_login(now)
            self._store.write(fresh)
            self._adopt(fresh)

    async def _password_login(self, now: float) -> SpondTokens:
        if not self._allow_password:
            raise AuthDead(SEED_HINT)
        blocked = self._store.blocked_until()
        if blocked and blocked > now:
            raise LoginBlocked(
                f"Spond-innlogging feilet nylig; venter {int(blocked - now)} s før nytt forsøk "
                "(for ikke å låse kontoen)."
            )
        try:
            tokens = await self._auth_api.login(
                self._client.clientsession, self._config.spond_username, self._config.spond_password
            )
        except TwoFactorRequired:
            raise AuthDead("Spond krever SMS-kode. " + SEED_HINT) from None
        except (LoginFailed, AccountLocked) as exc:
            self._store.block(now + LOGIN_COOLDOWN_S)
            raise LoginBlocked(f"Spond avviste innloggingen: {exc}") from None
        self.logins += 1
        return tokens

    def auth_status(self) -> dict:
        t = self._tokens or self._store.read()
        now = self._wall()
        out = {
            "mode": "passord som reserve" if self._allow_password else "kun refresh-token",
            "refreshes_this_process": self.refreshes,
            "password_logins_this_process": self.logins,
        }
        if t is None:
            out["token"] = "mangler — " + SEED_HINT
        else:
            out.update({
                "source": t.source,
                "access_expires_min": round((t.access_exp - now) / 60),
                "refresh_expires_days": round((t.refresh_exp - now) / 86400, 1),
            })
        return out

    async def _ensure_chat(self) -> None:
        """Chat-innlogging med statussjekk (bibliotekets ``_login_chat`` har ingen)."""
        s = self._client
        if s._auth and s._chat_url:
            return
        data = await self._request("POST", f"{s.api_url}chat", headers=s.auth_headers)
        if not isinstance(data, dict) or not data.get("url") or not data.get("auth"):
            raise SpondError("chat-innlogging: uventet svar")
        s._chat_url = data["url"]
        s._auth = data["auth"]

    # ------------------------------------------------------------------ #
    # Kall med fornying ved 401
    # ------------------------------------------------------------------ #
    async def _call(self, coro_factory, label: str, *, retry: bool = True):
        """Som ``SpondClient._call``, men fornyer tokenet én gang ved 401.

        ``retry=False`` hopper over backoff-forsøkene ved nettverksfeil. Brukes
        ved sending: et tidsavbrudd etter at Spond har tatt imot meldingen ville
        ellers gitt dobbel melding. En 401 betyr derimot at ingenting ble tatt
        imot, så ett nytt forsøk etter fornying er trygt også da.
        """
        for attempt in (1, 2):
            await self.ensure()
            used = self._client.token
            try:
                if retry:
                    return await super()._call(coro_factory, label)
                result = await coro_factory()
                await asyncio.sleep(CALL_SLEEP_SECONDS)
                return result
            except Exception as exc:  # noqa: BLE001 - filtreres under
                if attempt == 2 or not _is_auth_error(exc):
                    raise
            await self._ensure_token(stale=used)
            self._client._auth = None
            self._client._chat_url = None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request(self, method: str, url: str, *, headers: dict, json=None,
                       params=None, raw: bool = False):
        session = self._client.clientsession
        async with session.request(method, url, headers=headers, json=json, params=params) as r:
            if r.status == 401:
                raise AuthExpired(f"HTTP 401 fra {method} {url.split('/v1/')[-1]}")
            if not r.ok:
                body = (await r.text())[:200]
                raise SpondError(f"HTTP {r.status} fra {method} {url.split('/v1/')[-1]}: {body}")
            if raw:
                return await r.read()
            return await r.json(content_type=None)

    async def _core(self, method: str, path: str, *, label: str, retry: bool = True,
                    json=None, params=None, raw: bool = False):
        async def factory():
            s = self._client
            return await self._request(
                method, f"{s.api_url}{path}", headers=s.auth_headers,
                json=json, params=params, raw=raw,
            )
        return await self._call(factory, label, retry=retry)

    async def _chat(self, method: str, path: str, *, label: str, retry: bool = True,
                    json=None, params=None):
        async def factory():
            s = self._client
            await self._ensure_chat()
            return await self._request(
                method, f"{s._chat_url}{path}", headers={"auth": s._auth},
                json=json, params=params,
            )
        return await self._call(factory, label, retry=retry)

    # ------------------------------------------------------------------ #
    # Lesing (status sjekkes alltid)
    # ------------------------------------------------------------------ #
    async def fetch_profile(self) -> dict:
        return await self._core("GET", "profile", label="profile")

    async def fetch_groups(self) -> list[dict]:
        result = await self._core("GET", "groups/", label="groups")
        if not isinstance(result, list):
            raise SpondError("groups: uventet svar (ikke en liste)")
        return result

    async def fetch_event(self, event_id: str) -> dict:
        """Ferskt arrangement rett fra ``GET sponds/{id}`` (aldri bibliotekets cache)."""
        result = await self._core("GET", f"sponds/{event_id}", label=f"event[{event_id}]")
        if not isinstance(result, dict) or not result.get("id"):
            raise SpondError(f"event[{event_id}]: uventet svar")
        return result

    async def fetch_group_posts(self, group_id: str, max_posts: int = 50,
                                include_comments: bool = True) -> list[dict]:
        result = await self._core(
            "GET", "posts/", label=f"posts[{group_id}]",
            params={
                "type": "PLAIN", "max": str(max_posts), "groupId": group_id,
                "includeComments": str(include_comments).lower(),
            },
        )
        return result if isinstance(result, list) else []

    async def fetch_chats(self, max_chats: int = 50) -> list[dict]:
        result = await self._chat("GET", "/chats/", label="chats",
                                  params={"max": str(max_chats)})
        return result if isinstance(result, list) else []

    async def fetch_chat_messages(self, chat_id: str, max_messages: int = 40) -> list[dict]:
        result = await self._chat("GET", f"/chats/{chat_id}/messages",
                                  label=f"chat[{chat_id}]", params={"max": str(max_messages)})
        return result if isinstance(result, list) else []

    async def export_xlsx(self, event_id: str) -> bytes:
        data = await self._core("GET", f"sponds/{event_id}/export",
                                label=f"export[{event_id}]", raw=True)
        if not data.startswith(XLSX_MAGIC):
            raise SpondError(f"export[{event_id}]: svaret er ikke en xlsx-fil ({data[:60]!r})")
        return data

    # ------------------------------------------------------------------ #
    # Skriving
    # ------------------------------------------------------------------ #
    async def put_response(self, event_id: str, member_uid: str, accepted: bool,
                           decline_message: str | None = None) -> dict:
        """Sett svar for ett medlem. PUT er idempotent, så vanlig retry er trygt.

        Samme form som Spond sin web-klient: ``{"accepted": bool}``, pluss
        ``declineMessage`` bare når den ikke er tom.
        """
        payload: dict = {"accepted": accepted}
        if decline_message:
            payload["declineMessage"] = decline_message
        result = await self._core(
            "PUT", f"sponds/{event_id}/responses/{member_uid}",
            label=f"rsvp[{event_id}/{member_uid}]", json=payload,
        )
        return result if isinstance(result, dict) else {}

    async def send_chat(self, recipient_profile_id: str, group_id: str, text: str) -> dict:
        """Ny DM. Ingen retry ved nettverksfeil (kan gi dobbel melding)."""
        return await self._chat(
            "POST", "/messages", label="send_chat", retry=False,
            json={"text": text, "type": "TEXT", "recipient": recipient_profile_id,
                  "groupId": group_id},
        )

    async def reply_chat(self, chat_id: str, text: str) -> dict:
        """Svar i eksisterende chat. Ingen retry ved nettverksfeil."""
        return await self._chat(
            "POST", "/messages", label="reply_chat", retry=False,
            json={"chatId": chat_id, "text": text, "type": "TEXT"},
        )
