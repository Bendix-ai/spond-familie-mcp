"""Spond-MCP på nett: hele OAuth-flyten mot den ekte HTTP-appen (Starlette TestClient).

Ingen nettverk: Spond er en FakeSession, og «Claude» er testklienten.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient  # noqa: E402

from _mcp_fixtures import NOW, FakeSession, config  # noqa: E402

from spond_familie.oauth import OwnerOAuthProvider, hash_passphrase, redirect_allowed  # noqa: E402
from spond_familie.runtime import Runtime  # noqa: E402
from spond_familie.server import HttpOptions, _Edge, build_server  # noqa: E402
from spond_familie.tokens import _write_private  # noqa: E402

BASE = "https://h.test:8443"
RESOURCE = f"{BASE}/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASS = "riktig hest batteri stift"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOND_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SPOND_MCP_READONLY", raising=False)
    _write_private(tmp_path / "owner.json", hash_passphrase(PASS, n=2**10))
    rt = Runtime(config=config(), session_factory=lambda c: FakeSession(),
                 now=lambda: NOW, mode="remote")
    mcp = build_server(rt, http=HttpOptions(BASE, state_dir=tmp_path))
    return mcp.streamable_http_app(), tmp_path


@pytest.fixture
def client(app):
    with TestClient(app[0], base_url=BASE) as c:
        yield c


def _register(c, redirect=CALLBACK):
    return c.post("/register", json={
        "redirect_uris": [redirect], "client_name": "Claude", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _authorize(c, client_id, challenge, resource=RESOURCE):
    return c.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "scope": "spond offline_access", "resource": resource,
    }, follow_redirects=False)


def _login(c, login_url, passphrase=PASS):
    req = parse_qs(urlparse(login_url).query)["req"][0]
    return c.post("/owner/login", data={"req": req, "passphrase": passphrase},
                  headers={"Origin": BASE}, follow_redirects=False)


def _connect(c):
    """Kjør hele flyten slik claude.ai gjør; returner (client_id, tokens)."""
    client_id = _register(c).json()["client_id"]
    verifier, challenge = _pkce()
    login_url = _authorize(c, client_id, challenge).headers["location"]
    cb = _login(c, login_url).headers["location"]
    q = parse_qs(urlparse(cb).query)
    assert q["state"] == ["xyz"]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                 "redirect_uri": CALLBACK, "client_id": client_id,
                                 "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return client_id, tok.json()


def _rpc(c, access, method, params=None, id_=1):
    headers = {**MCP_HEADERS, **({"Authorization": f"Bearer {access}"} if access else {})}
    return c.post("/mcp", headers=headers,
                  json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


# --------------------------------------------------------------------------- #
def test_unauthenticated_mcp_points_to_metadata(client):
    r = _rpc(client, None, "tools/list")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == RESOURCE
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert "offline_access" in asm["scopes_supported"] and "S256" in asm["code_challenge_methods_supported"]


@pytest.mark.parametrize("uri,ok", [
    (CALLBACK, True), ("https://claude.com/api/mcp/auth_callback", True),
    ("http://localhost:53682/callback", True), ("http://127.0.0.1:9999/cb", True),
    ("https://evil.example/cb", False), ("http://localhost.evil.example/cb", False),
    ("http://localhost@evil.example/cb", False), ("https://claude.ai/annet", False),
])
def test_redirect_allowlist(uri, ok):
    assert redirect_allowed(uri) is ok


def test_registration_rejects_foreign_redirects(client):
    assert _register(client, "https://evil.example/cb").status_code == 400
    assert _register(client).status_code == 201


def test_full_flow_then_tool_call(client):
    _, tok = _connect(client)
    assert tok["expires_in"] == 3600 and "offline_access" in tok["scope"]
    init = _rpc(client, tok["access_token"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "spond"
    listed = _rpc(client, tok["access_token"], "tools/list", id_=2)
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"overview", "rsvp", "send_message"} <= names
    call = _rpc(client, tok["access_token"], "tools/call", {"name": "list_groups", "arguments": {}}, id_=3)
    body = json.loads(call.json()["result"]["content"][0]["text"])
    assert {g["short"] for g in body["groups"]} == {"Testlag J15", "Turngruppe"}


def test_audit_is_tagged_with_http_and_client(client, app):
    client_id, tok = _connect(client)
    _rpc(client, tok["access_token"], "tools/call",
         {"name": "rsvp", "arguments": {"event_id": "EVFUTURE", "child": "Kari", "answer": "ja"}})
    line = json.loads((app[1] / "home" / "audit.jsonl").read_text().splitlines()[-1])
    assert line["transport"] == "http" and line["client"] == client_id and line["phase"] == "preview"


def test_wrong_passphrase_and_lockout(client):
    client_id = _register(client).json()["client_id"]
    _, challenge = _pkce()
    login_url = _authorize(client, client_id, challenge).headers["location"]
    assert client.get(login_url).status_code == 200
    for _ in range(4):
        assert _login(client, login_url, "feil").status_code == 401
    assert _login(client, login_url, "feil").status_code == 429
    assert _login(client, login_url, PASS).status_code == 429  # sperret, også med riktig


def test_foreign_origin_is_rejected(client):
    client_id = _register(client).json()["client_id"]
    _, challenge = _pkce()
    login_url = _authorize(client, client_id, challenge).headers["location"]
    req = parse_qs(urlparse(login_url).query)["req"][0]
    r = client.post("/owner/login", data={"req": req, "passphrase": PASS},
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_wrong_resource_never_reaches_login(client):
    client_id = _register(client).json()["client_id"]
    _, challenge = _pkce()
    r = _authorize(client, client_id, challenge, resource="https://annen.example/mcp")
    assert "/owner/login" not in r.headers.get("location", "")


def test_refresh_rotates_and_old_tokens_die(client):
    client_id, tok = _connect(client)
    new = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": client_id})
    assert new.status_code == 200
    new = new.json()
    assert new["refresh_token"] != tok["refresh_token"]
    old = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": client_id})
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
    assert _rpc(client, tok["access_token"], "tools/list").status_code == 401
    assert _rpc(client, new["access_token"], "tools/list").status_code == 200


def test_revoke_kills_whole_grant(client):
    client_id, tok = _connect(client)
    # SDK-en (1.30) krever feltet client_secret også for klienter uten hemmelighet.
    r = client.post("/revoke", data={"token": tok["access_token"], "client_id": client_id,
                                     "client_secret": ""})
    assert r.status_code == 200
    assert _rpc(client, tok["access_token"], "tools/list").status_code == 401
    again = client.post("/token", data={"grant_type": "refresh_token",
                                        "refresh_token": tok["refresh_token"], "client_id": client_id})
    assert again.status_code == 400


def test_tokens_survive_restart(client, app):
    _, tok = _connect(client)
    tmp = app[1]
    fresh = OwnerOAuthProvider(tmp / "oauth.json", tmp / "owner.json", BASE, RESOURCE)
    import asyncio
    assert asyncio.run(fresh.load_access_token(tok["access_token"])) is not None
    stored = (tmp / "oauth.json").read_text()
    assert tok["access_token"] not in stored and tok["refresh_token"] not in stored
    assert oct((tmp / "oauth.json").stat().st_mode & 0o777) == "0o600"


def test_edge_serves_trailing_slash_metadata_and_https_redirects(app):
    edge = _Edge(app[0], public_host="h.test")
    with TestClient(edge, base_url="http://h.test:8443") as c:  # Funnel leverer http til appen
        r = c.get("/.well-known/oauth-authorization-server/", follow_redirects=False)
        assert r.status_code == 200 and r.json()["issuer"].startswith("https://h.test:8443")
        client_id = _register(c).json()["client_id"]
        _, challenge = _pkce()
        loc = _authorize(c, client_id, challenge).headers["location"]
        assert loc.startswith("https://h.test:8443/owner/login")


def test_login_page_lets_browser_send_real_origin(client):
    """Regresjon: «Referrer-Policy: no-referrer» får nettleseren til å sende
    «Origin: null» på skjemaet, og da ble riktig passfrase avvist med 403."""
    client_id = _register(client).json()["client_id"]
    _, challenge = _pkce()
    login_url = _authorize(client, client_id, challenge).headers["location"]
    page = client.get(login_url)
    assert page.headers["referrer-policy"] == "same-origin"
    req = parse_qs(urlparse(login_url).query)["req"][0]
    null_origin = client.post("/owner/login", data={"req": req, "passphrase": PASS},
                              headers={"Origin": "null"}, follow_redirects=False)
    assert null_origin.status_code == 403 and "Passfrasen ble ikke sjekket" in null_origin.text
