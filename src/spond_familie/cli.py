"""Kommandolinjen: ``spond-familie [login|status|setup-claude|tool|serve-http|owner-setup|revoke-all]``.

Uten argumenter starter MCP-serveren (stdio), slik Claude Desktop og Claude Code
kaller den. Alt annet er for deg i terminalen.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

WARN_DAYS = 7


# --------------------------------------------------------------------------- #
# login / status
# --------------------------------------------------------------------------- #
def _describe_token() -> int:
    from .tokens import TokenStore

    store = TokenStore()
    t = store.read()
    if t is None:
        print("Ikke innlogget. Kjør: spond-familie login")
        return 1
    now = time.time()
    access_h = (t.access_exp - now) / 3600
    refresh_d = (t.refresh_exp - now) / 86400
    print(f"Innlogging lagret i {store.path}")
    print(f"  hentet:   {datetime.fromtimestamp(t.obtained_at):%Y-%m-%d %H:%M} ({t.source})")
    print(f"  access:   {'utløpt (fornyes automatisk)' if access_h <= 0 else f'{access_h:.1f} t igjen'}")
    print(f"  refresh:  {'utløpt' if refresh_d <= 0 else f'{refresh_d:.0f} dager igjen'} "
          "(skyves 90 dager fram hver gang den brukes)")
    if refresh_d < WARN_DAYS:
        print("  ⚠ Snart utløpt. Kjør: spond-familie login")
        return 1
    return 0


async def _login(email: str, password: str) -> None:
    import aiohttp

    from .spond_auth import SpondAuthApi, TwoFactorRequired
    from .tokens import TokenStore

    store = TokenStore()
    api = SpondAuthApi()
    async with aiohttp.ClientSession() as http, store.locked():
        try:
            tokens = await api.login(http, email, password)
        except TwoFactorRequired as tfa:
            code = input(f"Spond sendte en SMS-kode til {tfa.phone or 'telefonen din'}. Kode: ").strip()
            tokens = await api.verify_2fa(http, tfa.token, code, tfa.phone)
        store.write(tokens)


def cmd_login(args) -> int:
    email = args.email or os.environ.get("SPOND_USERNAME") or input("Spond-e-post (eller telefonnummer): ").strip()
    password = os.environ.get("SPOND_PASSWORD") or getpass.getpass("Spond-passord (lagres ikke): ")
    try:
        asyncio.run(_login(email, password))
    except Exception as exc:  # noqa: BLE001 - vis en forståelig feil
        print(f"Innlogging feilet: {exc}")
        return 1
    print("✓ Innlogget. Passordet er ikke lagret, bare et token som fornyes automatisk.")
    return _describe_token()


def cmd_status(args) -> int:
    return _describe_token()


# --------------------------------------------------------------------------- #
# setup-claude
# --------------------------------------------------------------------------- #
def claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _executable() -> str:
    """Stien Claude skal starte. Følger ikke symlenker: ``~/.local/bin/spond-familie``
    (fra ``uv tool install``) er stabil på tvers av oppdateringer."""
    found = shutil.which("spond-familie")
    return str(Path(found).absolute()) if found else str(Path(sys.argv[0]).absolute())


def _is_ephemeral(path: str) -> bool:
    """``uvx``/cache-stier forsvinner når cachen ryddes, og Claude får da ikke startet serveren."""
    p = path.replace("\\", "/")
    return any(m in p for m in ("/archive-v", "/.cache/uv/", "/uv/cache/", "/builds-v"))


def cmd_setup_claude(args) -> int:
    exe = _executable()
    if _is_ephemeral(exe) and not args.force:
        print(f"✗ spond-familie kjører fra en midlertidig sti ({exe}).")
        print("  Installer først: uv tool install git+https://github.com/Bendix-ai/spond-familie-mcp")
        print("  og kjør så: spond-familie setup-claude")
        return 1
    path = Path(args.config) if args.config else claude_desktop_config_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"✗ {path} er ikke gyldig JSON. Rett den først (ingenting er endret).")
            return 1
        backup = path.with_name(path.name + ".bak-spond-familie")
        shutil.copy2(path, backup)
        print(f"  Sikkerhetskopi: {backup}")
    entry = {"command": exe}
    if args.readonly:
        entry["env"] = {"SPOND_MCP_READONLY": "1"}
    data.setdefault("mcpServers", {})["spond"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    print(f"✓ Lagt inn i Claude Desktop ({path}).")
    print("  Avslutt Claude Desktop helt (⌘Q) og åpne den igjen.")
    print()
    print("Bruker du Claude Code også? Kjør:")
    print(f"  claude mcp add --scope user spond -- {exe}")
    return 0


# --------------------------------------------------------------------------- #
# Fjerntilgang (avansert): eier-passfrase
# --------------------------------------------------------------------------- #
def cmd_owner_setup(args) -> int:
    from .oauth import hash_passphrase
    from .safety import state_home
    from .tokens import _write_private

    p1 = getpass.getpass("Ny passfrase for fjerntilgang (minst 12 tegn): ")
    if len(p1) < 12:
        print("For kort: minst 12 tegn.")
        return 1
    if getpass.getpass("Gjenta: ") != p1:
        print("Ulike. Ingenting er endret.")
        return 1
    _write_private(state_home() / "owner.json", hash_passphrase(p1))
    print(f"✓ Lagret (bare en hash) i {state_home() / 'owner.json'}.")
    return 0


def cmd_revoke_all(args) -> int:
    from .safety import state_home
    from .tokens import _read_json, _write_private

    path = state_home() / "oauth.json"
    data = _read_json(path) or {}
    n = len(data.get("refresh", {}))
    data["access"], data["refresh"] = {}, {}
    _write_private(path, data)
    print(f"✓ Tilbakekalte {n} tilkobling(er). Start serve-http på nytt (den holder tokener i minnet).")
    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .server import serve_stdio

        serve_stdio()
        return

    from . import __version__

    p = argparse.ArgumentParser(
        prog="spond-familie",
        description="Koble Claude til din egen Spond. Uten argumenter: start MCP-serveren (stdio).",
    )
    p.add_argument("--version", action="version", version=f"spond-familie {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="logg inn i Spond én gang (passordet lagres ikke)")
    s.add_argument("--email", help="Spond-e-post eller telefonnummer")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("status", help="vis om du er innlogget og når innloggingen utløper")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("setup-claude", help="legg spond-familie inn i Claude Desktop")
    s.add_argument("--readonly", action="store_true", help="uten svar/meldinger (bare lesing)")
    s.add_argument("--config", help="annen sti til claude_desktop_config.json")
    s.add_argument("--force", action="store_true", help="bruk gjeldende sti selv om den ser midlertidig ut")
    s.set_defaults(fn=cmd_setup_claude)

    s = sub.add_parser("tool", help="kjør ett verktøy fra terminalen (for feilsøking)")
    s.add_argument("name")
    s.add_argument("args_json", nargs="?", help="argumenter som JSON, f.eks. '{\"days\": 3}'")
    s.set_defaults(fn=lambda a: __import__("spond_familie.server", fromlist=["x"]).run_tool_cli(a.name, a.args_json))

    s = sub.add_parser("serve-http", help="(avansert) HTTP+OAuth for claude.ai og mobil")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--public-url", default=os.environ.get("SPOND_MCP_PUBLIC_URL"))
    s.set_defaults(fn=lambda a: __import__("spond_familie.server", fromlist=["x"]).serve_http(a.port, a.public_url))

    s = sub.add_parser("owner-setup", help="(avansert) passfrase for fjerntilgang")
    s.set_defaults(fn=cmd_owner_setup)

    s = sub.add_parser("revoke-all", help="(avansert) logg ut alle fjerntilkoblinger")
    s.set_defaults(fn=cmd_revoke_all)

    a = p.parse_args(argv)
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
