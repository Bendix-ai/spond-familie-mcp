"""Sperrer for skriving: forhåndsvisning→bekreftelse, regler og revisjonslogg.

Alle skriveverktøy går i to steg. Uten ``confirm`` gjør verktøyet bare en
forhåndsvisning mot ferske data og returnerer en ``preview_id``. Med
``confirm=true`` må samme parametre og en ubrukt, ikke-utløpt ``preview_id``
følge med; alle sjekker kjøres på nytt rett før utførelse.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

PREVIEW_TTL_S = 600

# Settes av server.py per verktøykall: {"transport": "stdio"|"http", "client": client_id}.
AUDIT_CONTEXT: ContextVar[dict] = ContextVar("spond_familie_audit", default={"transport": "cli"})


class SafetyError(ValueError):
    """Brukerrettet avvisning av en skriving."""


def _digest(tool: str, params: dict) -> str:
    canon = json.dumps({"tool": tool, **params}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class PreviewStore:
    """Engangs-forhåndsvisninger i minnet, bundet til verktøy og parametre."""

    def __init__(self, *, ttl_s: int = PREVIEW_TTL_S, clock=time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._items: dict[str, tuple[str, float]] = {}

    def create(self, tool: str, params: dict) -> str:
        now = self._clock()
        self._items = {k: v for k, v in self._items.items() if v[1] > now}
        pid = secrets.token_hex(4)
        self._items[pid] = (_digest(tool, params), now + self._ttl)
        return pid

    def consume(self, preview_id: str | None, tool: str, params: dict) -> None:
        if not preview_id:
            raise SafetyError(
                "confirm=true krever preview_id fra en forhåndsvisning. "
                "Kjør uten confirm først, vis brukeren forhåndsvisningen og få et ja."
            )
        item = self._items.pop(preview_id, None)
        if item is None:
            raise SafetyError("Ukjent eller allerede brukt preview_id. Lag en ny forhåndsvisning.")
        digest, expires = item
        if self._clock() > expires:
            raise SafetyError("Forhåndsvisningen er utløpt (10 min). Lag en ny.")
        if digest != _digest(tool, params):
            raise SafetyError("Parametrene er endret siden forhåndsvisningen. Lag en ny.")


# --------------------------------------------------------------------------- #
# Revisjonslogg
# --------------------------------------------------------------------------- #
def state_home() -> Path:
    """``SPOND_MCP_HOME``, ellers ``~/.local/state/spond-familie`` (Windows: ``%LOCALAPPDATA%``)."""
    env = os.environ.get("SPOND_MCP_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "spond-familie"
    return Path.home() / ".local" / "state" / "spond-familie"


def audit_path() -> Path:
    return state_home() / "audit.jsonl"


def audit(entry: dict) -> None:
    """Legg én linje til ``audit.jsonl`` (mappe 0700, fil 0600, O_APPEND)."""
    home = state_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid(),
         **AUDIT_CONTEXT.get(), **entry},
        ensure_ascii=False, default=str,
    ) + "\n"
    fd = os.open(audit_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Regler
# --------------------------------------------------------------------------- #
def rsvp_checks(event: dict, member_id: str, now: datetime) -> tuple[list[str], list[str]]:
    """Returner ``(blokkere, advarsler)`` for å svare på vegne av ``member_id``."""
    from .serialize import _parse_iso, member_status  # unngå syklisk import

    blocks: list[str] = []
    warnings: list[str] = []
    if event.get("cancelled") is True:
        blocks.append("Arrangementet er avlyst.")
    start = _parse_iso(event.get("startTimestamp"))
    if start is not None and start <= now:
        blocks.append("Arrangementet har allerede startet.")
    invite = _parse_iso(event.get("inviteTime"))
    if invite is not None and invite > now:
        blocks.append("Påmeldingen er ikke åpnet ennå (invitasjonen er ikke sendt).")
    if member_status(event, member_id) is None:
        blocks.append("Barnet er ikke invitert til dette arrangementet.")
    deadline = _parse_iso(event.get("rsvpDate"))
    if deadline is not None and deadline < now:
        warnings.append("Svarfristen har gått ut; Spond kan avvise eller arrangøren kan overse svaret.")
    if event.get("autoAccept") is True:
        warnings.append("Arrangementet har automatisk påmelding (autoAccept).")
    return blocks, warnings


MESSAGE_MIN = 1
MESSAGE_MAX = 2000
MAX_RECIPIENTS = 4


def check_message_text(text: str) -> str:
    text = (text or "").strip()
    if not (MESSAGE_MIN <= len(text) <= MESSAGE_MAX):
        raise SafetyError(f"Meldingen må være {MESSAGE_MIN}–{MESSAGE_MAX} tegn.")
    return text
