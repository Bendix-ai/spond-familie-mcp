"""Grunnmur: innstillinger, en tynn Spond-klient og små hjelpere.

Spond har ikke noe offentlig API. Dette bygger på det uoffisielle
``spond``-biblioteket (Olen/Spond, GPL-3.0) og legger til det en langlivet
MCP-server trenger: retry med backoff, skånsom takt mot Spond, og henting i
tidsvinduer slik at lange perioder ikke kuttes av Spond sin grense per kall.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from spond import spond

WINDOW_STEP_DAYS = 70
CALL_SLEEP_SECONDS = 0.7
RETRY_BACKOFFS = (2, 4, 8)
MAX_EVENT_COMMENT_FETCH = 25
_RETRYABLE = (aiohttp.ClientError, asyncio.TimeoutError)

# Svarlistene som til sammen utgjør «invitert» til ett arrangement.
SVARLISTER = ("acceptedIds", "declinedIds", "unansweredIds", "waitinglistIds", "unconfirmedIds")


@dataclass(frozen=True)
class Settings:
    """Påloggingsdata. Brukes bare av ``spond-familie login`` (eller som reserve når
    miljøvariablene er satt). Serveren lever ellers på refresh-tokenet."""

    spond_username: str = ""
    spond_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(os.environ.get("SPOND_USERNAME", ""), os.environ.get("SPOND_PASSWORD", ""))


class SpondClient:
    """Asynkron kontekstbehandler rundt ``spond.Spond`` med retry og takt."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._spond: spond.Spond | None = None

    async def __aenter__(self) -> "SpondClient":
        self._spond = spond.Spond(username=self._config.spond_username,
                                  password=self._config.spond_password)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._spond is not None:
            session = self._spond.clientsession
            if session is not None:
                await session.close()
            self._spond = None

    @property
    def _client(self) -> spond.Spond:
        if self._spond is None:
            raise RuntimeError("SpondClient må brukes som async kontekstbehandler.")
        return self._spond

    async def _call(self, coro_factory, label: str):
        """Kjør et API-kall med retry/backoff og en liten pause etterpå."""
        last_exc: Exception | None = None
        for backoff in (*RETRY_BACKOFFS, None):
            try:
                result = await coro_factory()
                await asyncio.sleep(CALL_SLEEP_SECONDS)
                return result
            except _RETRYABLE as exc:
                last_exc = exc
                if backoff is None:
                    break
                await asyncio.sleep(backoff)
        raise RuntimeError(f"{label} feilet etter {len(RETRY_BACKOFFS) + 1} forsøk: {last_exc}")

    async def fetch_events_window(self, group_id: str, start: datetime, end: datetime) -> list[dict]:
        """Arrangementer for én gruppe, hentet i 70-dagersvinduer og slått sammen på id."""
        seen: dict[str, dict] = {}
        step = timedelta(days=WINDOW_STEP_DAYS)
        cursor = start
        while cursor < end:
            wend = min(cursor + step, end)

            def _factory(_min=cursor, _max=wend):
                return self._client.get_events(
                    group_id=group_id, include_scheduled=True, include_hidden=True,
                    min_start=_min, max_start=_max, max_events=300,
                )

            for event in await self._call(_factory, f"get_events[{group_id}]") or []:
                seen[event["id"]] = event
            cursor = wend
        return list(seen.values())

    async def fetch_event_comments(self, event_ids: list[str]) -> tuple[dict[str, list[dict]], list[str]]:
        """Ekte arrangementskommentarer (``get_events`` gir alltid tomt ``comments``).

        ``GET /core/v1/sponds/{id}/comments``. Maks 25 per kall; feil per
        arrangement isoleres i ``errors``.
        """
        unique_ids = list(dict.fromkeys(eid for eid in event_ids if eid))[:MAX_EVENT_COMMENT_FETCH]
        by_event: dict[str, list[dict]] = {}
        errors: list[str] = []
        base = self._client.api_url
        for eid in unique_ids:
            async def _factory(_url=f"{base}sponds/{eid}/comments"):
                async with self._client.clientsession.get(_url, headers=self._client.auth_headers) as resp:
                    if not resp.ok:
                        raise RuntimeError(f"HTTP {resp.status}")
                    return await resp.json()

            try:
                result = await self._call(_factory, f"event_comments[{eid}]")
                by_event[eid] = result if isinstance(result, list) else []
            except Exception as exc:  # noqa: BLE001 - isoler per arrangement
                errors.append(f"event_comments[{eid}]: {exc}")
        return by_event, errors


# --------------------------------------------------------------------------- #
# Hjelpere
# --------------------------------------------------------------------------- #
def parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def location_str(event: dict) -> str:
    loc = event.get("location") or {}
    return ", ".join(x for x in (loc.get("feature"), loc.get("address")) if x) if loc else ""


def flatten_comments(comments, depth: int = 0) -> list[dict]:
    """Flat liste av et Spond-kommentartre (svar ligger i ``children``)."""
    out: list[dict] = []
    for c in comments or []:
        out.append({
            "id": c.get("id"),
            "fromProfileId": c.get("fromProfileId"),
            "timestamp": c.get("timestamp"),
            "text": c.get("text"),
            "depth": depth,
            "image_url": (c.get("image") or {}).get("url"),
        })
        out.extend(flatten_comments(c.get("children"), depth + 1))
    return out


def chat_msg_text(msg: dict) -> str:
    return msg.get("text") or (msg.get("message") or {}).get("text") or ""


def chat_msg_ts(msg: dict):
    return msg.get("timestamp") or (msg.get("message") or {}).get("timestamp")


def normalize_child_name(name: str | None) -> str:
    """«Fornavn Mellomnavn Etternavn» → «fornavn etternavn» (samme barn har ulike
    mellomnavn i ulike grupper)."""
    parts = str(name or "").lower().split()
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1]}"
