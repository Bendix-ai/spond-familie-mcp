"""Spond-MCP: verktøyene og MCP-serveren (stdio lokalt, HTTP+OAuth for fjerntilgang).

Starter du ``spond-familie`` uten argumenter, kjører stdio-serveren som Claude Desktop
og Claude Code bruker. Se ``cli.py`` for ``login``, ``status``, ``setup-claude`` m.m.

stdout er MCP-protokollen i stdio-modus. Ingenting her skal printe dit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import tools
from .oauth import SCOPE, SCOPES, OwnerOAuthProvider, register_login_routes
from .runtime import Runtime
from .safety import AUDIT_CONTEXT, state_home
from .serialize import dumps

DEFAULT_PORT = 8765
KEEPALIVE_S = 6 * 3600

INSTRUCTIONS = """\
Spond for brukeren: grupper, arrangementer, svar, innlegg og chat, pluss trenerverktøy.
- Alle tider er Europe/Oslo og skrevet som «2026-10-14 (tir) 17:30».
- Start med overview for «hva skjer». Bruk list_events/get_event for detaljer.
- «ja» i statistikk betyr «svart ja», ikke «møtte».
- rsvp og send_message går i to steg. Kall først uten confirm og vis forhåndsvisningen
  ordrett. Kall med confirm=true og preview_id BARE når brukeren tydelig har sagt ja i
  denne samtalen. Aldri bekreft på eget initiativ.
- Ikke del navn på andres barn utenfor det brukeren ber om."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                             openWorldHint=True)
RSVP = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                       openWorldHint=True)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                       openWorldHint=True)


@dataclass(frozen=True)
class HttpOptions:
    public_url: str            # f.eks. https://<mac>.<tailnet>.ts.net:8443
    state_dir: Path | None = None

    @property
    def base(self) -> str:
        return self.public_url.rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.base}/mcp"


# --------------------------------------------------------------------------- #
# Verktøy (samme 13 i begge moduser)
# --------------------------------------------------------------------------- #
def register_tools(mcp: FastMCP, rt: Runtime, *, readonly: bool, transport: str) -> None:
    async def _run(fn, **kwargs) -> str:
        token = get_access_token()
        ctx = AUDIT_CONTEXT.set({"transport": transport, "client": token.client_id if token else None})
        try:
            return dumps(await rt.run(fn, **kwargs))
        finally:
            AUDIT_CONTEXT.reset(ctx)

    @mcp.tool(annotations=READ, structured_output=False)
    async def overview(days: int = 7) -> str:
        """Startpunktet: hva skjer de neste `days` dagene i alle grupper.

        Gir barnas ubesvarte arrangementer og svarfrister (neste 14 dager), en
        kompakt kalender og avlysninger. Bruk get_event(id) for detaljer.
        """
        return await _run(tools.overview, days=days)

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_groups() -> str:
        """Alle Spond-grupper: short-navn, mine roller, coach (har rolle), barn og antall medlemmer."""
        return await _run(tools.list_groups)

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_events(group: str | None = None, days_ahead: int = 14, days_back: int = 0,
                          query: str | None = None, only_children: bool = False, limit: int = 50) -> str:
        """Arrangementer i en gruppe (eller alle), med svartall og barnas status.

        group: id, navn eller del av navnet (f.eks. «J2015»); tom = alle grupper.
        days_back > 0 tar med tidligere arrangementer. query filtrerer på tittel/beskrivelse.
        only_children: bare arrangementer der et av barna er invitert.
        """
        return await _run(tools.list_events, group=group, days_ahead=days_ahead, days_back=days_back,
                          query=query, only_children=only_children, limit=limit)

    @mcp.tool(annotations=READ, structured_output=False)
    async def get_event(event_id: str, include_comments: bool = True, include_chat: bool = False) -> str:
        """Alt om ett arrangement: beskrivelse, vedlegg, hvem som har svart hva (med navn der
        du er trener eller lista er åpen), avmeldingsgrunner (trener), kommentarer, koblet chat,
        og hvilke av barna du kan svare for (can_rsvp_for)."""
        return await _run(tools.get_event, event_id=event_id, include_comments=include_comments,
                          include_chat=include_chat)

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_posts(group: str | None = None, days: int = 14, query: str | None = None,
                         include_comments: bool = False, limit: int = 20) -> str:
        """Veggoppslag fra de siste `days` dagene, nyeste først. group tom = alle grupper."""
        return await _run(tools.list_posts, group=group, days=days, query=query,
                          include_comments=include_comments, limit=limit)

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_chats(limit: int = 20, unread_only: bool = False) -> str:
        """Chatter (DM, gruppe, klubb) med siste melding, nyeste først."""
        return await _run(tools.list_chats, limit=limit, unread_only=unread_only)

    @mcp.tool(annotations=READ, structured_output=False)
    async def get_chat(chat_id: str, limit: int = 40) -> str:
        """Meldingene i én chat (chat_id fra list_chats), eldst først."""
        return await _run(tools.get_chat, chat_id=chat_id, limit=limit)

    @mcp.tool(annotations=READ, structured_output=False)
    async def find_person(query: str, group: str | None = None) -> str:
        """Finn medlem eller foresatt på navn: gruppe, rolle, foresatte og om det er ditt barn.
        Viser aldri telefon eller e-post."""
        return await _run(tools.find_person, query=query, group=group)

    @mcp.tool(annotations=READ, structured_output=False)
    async def attendance_stats(group: str, days_back: int = 90, kind: str = "all",
                               member: str | None = None) -> str:
        """Svarstatistikk per medlem over avsluttede arrangementer (trenerverktøy).

        kind: all | match | training. member: filtrer på navn.
        Tallet er «svart ja», ikke oppmøte; «attended» vises bare der oppmøte er ført.
        I grupper der du ikke har rolle vises bare dine barn.
        """
        return await _run(tools.attendance_stats, group=group, days_back=days_back, kind=kind,
                          member=member)

    @mcp.tool(annotations=LOCAL_FILE, structured_output=False)
    async def export_attendance_xlsx(event_id: str) -> str:
        """Last ned Sponds oppmøte-Excel for ett arrangement til ~/Spond-eksport på maskinen
        serveren kjører på (bare i grupper der du har rolle). Returnerer filsti og et
        sammendrag uten kontaktdata."""
        return await _run(tools.export_attendance_xlsx, event_id=event_id)

    @mcp.tool(annotations=READ, structured_output=False)
    async def status() -> str:
        """Helsesjekk: modus, Spond-innlogging (utløp, aldri tokener), cache, lesemodus, siste feil."""
        return await _run(tools.status)

    if readonly:
        return

    @mcp.tool(annotations=RSVP, structured_output=False)
    async def rsvp(event_id: str, child: str, answer: str, confirm: bool = False,
                   preview_id: str | None = None, message: str | None = None) -> str:
        """Svar ja/nei for et av barna dine på et arrangement. TO STEG.

        answer: accept | decline. child: barnets fornavn.
        message: valgfri avmeldingsgrunn (bare ved decline, maks 200 tegn), vises for treneren.
        1) Kall uten confirm → forhåndsvisning med preview_id. Vis den til brukeren.
        2) Kall med samme argumenter + confirm=true + preview_id KUN etter tydelig ja.
        """
        return await _run(tools.rsvp, event_id=event_id, child=child, answer=answer,
                          confirm=confirm, preview_id=preview_id, message=message)

    @mcp.tool(annotations=SEND, structured_output=False)
    async def send_message(text: str, group: str | None = None, to: str | None = None,
                           audience: str = "guardians", chat_id: str | None = None,
                           confirm: bool = False, preview_id: str | None = None) -> str:
        """Send Spond-melding i en gruppe der du har rolle (trener/lagleder). TO STEG.

        Ny DM: group + to (navn på spiller eller foresatt) + audience
        (guardians | member | both; hver mottaker får egen DM, maks 4).
        Svar i eksisterende chat: chat_id (fra list_chats).
        1) Uten confirm → forhåndsvisning med mottakere, tekst og preview_id. Vis ordrett.
        2) Samme argumenter + confirm=true + preview_id KUN etter tydelig ja.
        Feilet sending prøves ikke på nytt.
        """
        return await _run(tools.send_message, text=text, group=group, to=to, audience=audience,
                          chat_id=chat_id, confirm=confirm, preview_id=preview_id)


# --------------------------------------------------------------------------- #
# Bygging
# --------------------------------------------------------------------------- #
def build_server(rt: Runtime, *, http: HttpOptions | None = None) -> FastMCP:
    if http is None:
        @asynccontextmanager
        async def _lifespan(_server):
            try:
                yield {}
            finally:
                await rt.aclose()

        mcp = FastMCP("spond", instructions=INSTRUCTIONS, lifespan=_lifespan)
        register_tools(mcp, rt, readonly=rt.readonly, transport="stdio")
        return mcp

    state = http.state_dir or state_home()
    provider = OwnerOAuthProvider(state / "oauth.json", state / "owner.json", http.base, http.resource)
    parsed = urlparse(http.base)
    host, hostname = parsed.netloc, parsed.hostname
    port = parsed.port or 443
    # NB: lifespan kjøres per MCP-økt i HTTP-modus, så den kan ikke lukke Spond-økta.
    mcp = FastMCP(
        "spond",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=http.base,
            resource_server_url=http.resource,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=True,
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            allowed_hosts=sorted({host, hostname, f"{hostname}:{port}", "127.0.0.1:*", "localhost:*"}),
            allowed_origins=["https://claude.ai", "https://claude.com", http.base],
        ),
    )
    mcp._spond_oauth = provider  # for tester og nødbryter
    register_login_routes(mcp, provider)
    register_tools(mcp, rt, readonly=rt.readonly, transport="http")
    return mcp


class _Edge:
    """Inngangen bak Tailscale Funnel.

    - Funnel avslutter TLS, så appen ser ``http``. For forespørsler til den
      offentlige adressen settes ``scheme=https``, ellers peker genererte
      omdirigeringer til ``http://…:8443`` (som ikke virker).
    - ``/.well-known/...`` med skråstrek til slutt serveres direkte i stedet for
      en 307-omdirigering (RFC 8414-oppslag kan bygge stien med ``/`` til slutt
      når utstederen er ``https://vert:8443/``).
    - Logger metode, sti (uten spørrestreng) og status til stderr. Aldri tokener.
    """

    def __init__(self, app, public_host: str | None = None) -> None:
        self.app = app
        self.public_host = public_host

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
        if self.public_host and host.split(":")[0] == self.public_host:
            scope = {**scope, "scheme": "https"}
        path = scope["path"]
        if path.startswith("/.well-known/") and path.endswith("/"):
            path = path.rstrip("/")
            scope = {**scope, "path": path, "raw_path": path.encode()}
        seen = {}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                seen["status"] = msg["status"]
            await send(msg)

        t0 = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {scope['method']} {path} "
                  f"{seen.get('status', '-')} {ms}ms", file=sys.stderr, flush=True)


async def _keepalive(rt: Runtime) -> None:
    """Hold Spond-tokenet friskt så 90-dagersvinduet alltid glir, også uten bruk."""
    while True:
        await asyncio.sleep(KEEPALIVE_S)
        try:
            async with rt.lock:
                await (await rt.session()).ensure()
        except Exception as exc:  # noqa: BLE001
            print(f"keepalive: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


async def _serve_http(port: int, public_url: str) -> None:
    import uvicorn

    rt = Runtime(mode="remote")
    app = _Edge(build_server(rt, http=HttpOptions(public_url)).streamable_http_app(),
                public_host=urlparse(public_url).hostname)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                           log_level="warning", proxy_headers=False))
    keep = asyncio.create_task(_keepalive(rt))
    print(f"Spond-MCP (HTTP) på 127.0.0.1:{port}, offentlig {public_url}/mcp "
          f"({'lesemodus' if rt.readonly else 'lese + skrive'})", file=sys.stderr, flush=True)
    try:
        await server.serve()
    finally:
        keep.cancel()
        await rt.aclose()


# --------------------------------------------------------------------------- #
# Innganger (brukes av cli.py)
# --------------------------------------------------------------------------- #
def run_tool_cli(name: str, args_json: str | None) -> int:
    """Kjør ett verktøy fra terminalen. Kan aldri bekrefte en skriving."""
    names = {fn.__name__: fn for fn in tools.READ_TOOLS + tools.WRITE_TOOLS}
    if name not in names:
        print("Verktøy: " + ", ".join(names), file=sys.stderr)
        return 2
    kwargs = json.loads(args_json) if args_json else {}
    if kwargs.get("confirm"):
        print("Terminalen kan ikke bekrefte skrivinger. Bruk Claude.", file=sys.stderr)
        return 2
    rt = Runtime()

    async def go():
        try:
            return await rt.run(names[name], **kwargs)
        finally:
            await rt.aclose()

    try:
        result = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - vis feilen slik Claude ville sett den
        print(f"Feil ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(json.loads(dumps(result)), ensure_ascii=False, indent=2))
    return 0


def serve_http(port: int, public_url: str | None) -> int:
    if os.environ.get("SPOND_PASSWORD"):
        print("Nekter: SPOND_PASSWORD finnes i miljøet. HTTP-modus skal bare bruke refresh-token.",
              file=sys.stderr)
        return 2
    if not public_url or not public_url.startswith("https://"):
        print("Oppgi den offentlige https-adressen med --public-url (eller SPOND_MCP_PUBLIC_URL).",
              file=sys.stderr)
        return 2
    asyncio.run(_serve_http(port, public_url))
    return 0


def serve_stdio() -> None:
    build_server(Runtime()).run()
