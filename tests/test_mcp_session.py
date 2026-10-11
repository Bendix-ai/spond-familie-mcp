"""Spond-MCP: SpondSession med delt token-lager (refresh, rotasjon, passord-reserve)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _mcp_fixtures import config

import spond_familie._base as client_mod
import spond_familie.session as session_mod
from spond_familie.session import (
    REFRESH_MARGIN_S,
    AuthDead,
    AuthExpired,
    LoginBlocked,
    SpondError,
    SpondSession,
)
from spond_familie.spond_auth import (
    AccountLocked,
    LoginFailed,
    RefreshRejected,
    TwoFactorRequired,
    parse_tokens,
)
from spond_familie.tokens import SpondTokens, TokenStore

T0 = 1_800_000_000.0


class _FakeHttp:
    closed = False

    async def close(self):
        self.closed = True


class _FakeSpond:
    def __init__(self):
        self.token = None
        self._auth = "chat-auth"
        self._chat_url = "https://chat.example"
        self.api_url = "https://api.example/core/v1/"
        self.clientsession = _FakeHttp()


class FakeAuth:
    """Spond sitt auth-API: roterer refresh-token, ett bruk per token."""

    def __init__(self, now):
        self.now = now
        self.n = 0
        self.valid_refresh: set[str] = set()
        self.logins = 0
        self.refreshes = 0
        self.login_error: Exception | None = None

    def _issue(self, source):
        self.n += 1
        rt = f"R{self.n}"
        self.valid_refresh.add(rt)
        return SpondTokens(access=f"A{self.n}", access_exp=self.now() + 86400, refresh=rt,
                           refresh_exp=self.now() + 90 * 86400, obtained_at=self.now(), source=source)

    async def login(self, http, email, pw):
        self.logins += 1
        if self.login_error:
            raise self.login_error
        return self._issue("login")

    async def refresh(self, http, rt):
        self.refreshes += 1
        if rt not in self.valid_refresh:
            raise RefreshRejected("brukt")
        self.valid_refresh.discard(rt)
        return self._issue("refresh")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod, "CALL_SLEEP_SECONDS", 0)
    monkeypatch.setattr(session_mod, "CALL_SLEEP_SECONDS", 0)


@pytest.fixture
def clock():
    return [T0]


@pytest.fixture
def auth(clock):
    return FakeAuth(lambda: clock[0])


def _session(tmp_path: Path, auth, clock, *, allow_password=True, store=None) -> SpondSession:
    s = SpondSession(config(tmp_path), store=store or TokenStore(tmp_path / "token.json"),
                     allow_password=allow_password, auth_api=auth, wallclock=lambda: clock[0])
    s._spond = _FakeSpond()
    return s


def _ok():
    async def factory():
        return "ok"
    return factory


# --------------------------------------------------------------------------- #
# Lager og første innlogging
# --------------------------------------------------------------------------- #
def test_first_call_logs_in_once_and_persists_private_file(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    assert asyncio.run(s._call(_ok(), "x")) == "ok"
    assert auth.logins == 1 and s._spond.token == "A1"
    stored = TokenStore(tmp_path / "token.json").read()
    assert stored.refresh == "R1" and stored.source == "login"
    assert oct((tmp_path / "token.json").stat().st_mode & 0o777) == "0o600"


def test_new_process_reuses_disk_tokens_without_login(tmp_path, auth, clock):
    asyncio.run(_session(tmp_path, auth, clock)._call(_ok(), "x"))
    b = _session(tmp_path, auth, clock)
    asyncio.run(b._call(_ok(), "x"))
    assert auth.logins == 1 and auth.refreshes == 0 and b._spond.token == "A1"


def test_refresh_when_less_than_margin_left(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    asyncio.run(s._call(_ok(), "x"))
    clock[0] += 86400 - REFRESH_MARGIN_S + 60  # under 1 t igjen
    asyncio.run(s._call(_ok(), "x"))
    assert auth.refreshes == 1 and auth.logins == 1
    stored = TokenStore(tmp_path / "token.json").read()
    assert stored.source == "refresh" and stored.refresh == "R2" and s._spond.token == "A2"


def test_401_triggers_one_refresh_then_retry(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    calls = []

    async def factory():
        calls.append(s._spond.token)
        if len(calls) == 1:
            raise AuthExpired("HTTP 401")
        return "ok"

    assert asyncio.run(s._call(factory, "x")) == "ok"
    assert calls == ["A1", "A2"] and auth.refreshes == 1 and auth.logins == 1


def test_library_style_401_also_triggers_refresh(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    n = []

    async def factory():
        n.append(1)
        if len(n) == 1:
            raise ValueError("Request failed with status 401: expired")
        return [1]

    assert asyncio.run(s._call(factory, "x")) == [1]
    assert auth.refreshes == 1


def test_rotation_race_between_two_processes_uses_one_refresh(tmp_path, auth, clock):
    a = _session(tmp_path, auth, clock)
    b = _session(tmp_path, auth, clock)
    asyncio.run(a._call(_ok(), "x"))
    asyncio.run(b._call(_ok(), "x"))  # begge har A1/R1
    asyncio.run(a.relogin())          # A får 401 og fornyer: R1 brukt, R2 på disk
    assert auth.refreshes == 1

    async def b_401_once():
        if b._spond.token == "A1":
            raise AuthExpired("HTTP 401")
        return b._spond.token

    assert asyncio.run(b._call(b_401_once, "x")) == "A2"
    assert auth.refreshes == 1  # B tok As token fra disk i stedet for å bruke R1 igjen


def test_second_401_is_raised(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)

    async def factory():
        raise AuthExpired("HTTP 401")

    with pytest.raises(AuthExpired):
        asyncio.run(s._call(factory, "x"))


# --------------------------------------------------------------------------- #
# Kun refresh-token (nett-eksponert) og passord-reserve
# --------------------------------------------------------------------------- #
def test_refresh_only_with_dead_token_never_uses_password(tmp_path, auth, clock):
    store = TokenStore(tmp_path / "token.json")
    store.write(SpondTokens("A0", T0 + 60, "DEAD", T0 + 86400, T0, "refresh"))
    s = _session(tmp_path, auth, clock, allow_password=False, store=store)
    with pytest.raises(AuthDead, match="spond-familie login"):
        asyncio.run(s._call(_ok(), "x"))
    assert auth.logins == 0 and auth.refreshes == 1


def test_refresh_only_without_any_token(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock, allow_password=False)
    with pytest.raises(AuthDead):
        asyncio.run(s._call(_ok(), "x"))
    assert auth.logins == 0


def test_password_fallback_when_refresh_rejected(tmp_path, auth, clock):
    store = TokenStore(tmp_path / "token.json")
    store.write(SpondTokens("A0", T0 + 60, "DEAD", T0 + 86400, T0, "refresh"))
    s = _session(tmp_path, auth, clock, store=store)
    assert asyncio.run(s._call(_ok(), "x")) == "ok"
    assert auth.logins == 1 and store.read().source == "login"


def test_login_breaker_is_shared_between_processes(tmp_path, auth, clock):
    auth.login_error = LoginFailed("feil passord")
    a = _session(tmp_path, auth, clock)
    with pytest.raises(LoginBlocked, match="avviste"):
        asyncio.run(a._call(_ok(), "x"))
    b = _session(tmp_path, auth, clock)
    with pytest.raises(LoginBlocked, match="venter"):
        asyncio.run(b._call(_ok(), "x"))
    assert auth.logins == 1
    clock[0] += 601
    auth.login_error = None
    assert asyncio.run(b._call(_ok(), "x")) == "ok"
    assert auth.logins == 2


def test_two_factor_gives_seed_hint(tmp_path, auth, clock):
    auth.login_error = TwoFactorRequired("tok", "+47")
    s = _session(tmp_path, auth, clock)
    with pytest.raises(AuthDead, match="SMS"):
        asyncio.run(s._call(_ok(), "x"))


def test_no_retry_on_timeout_when_sending(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    n = []

    async def factory():
        n.append(1)
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(s._call(factory, "send", retry=False))
    assert n == [1]


# --------------------------------------------------------------------------- #
# Biblioteket, økta og eksport
# --------------------------------------------------------------------------- #
def test_library_login_is_routed_through_store(tmp_path, auth, clock):
    store = TokenStore(tmp_path / "token.json")
    store.write(SpondTokens("A9", T0 + 86400, "R9", T0 + 86400 * 90, T0, "refresh"))
    s = SpondSession(config(tmp_path), store=store, allow_password=True, auth_api=auth,
                     wallclock=lambda: clock[0])

    async def go():
        await s.open()
        await s._spond.login()  # det biblioteket kaller i require_authentication
        token = s._spond.token
        await s.close()
        return token

    assert asyncio.run(go()) == "A9"
    assert auth.logins == 0 and auth.refreshes == 0


def test_closed_session_is_recreated(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    fake = s._spond
    fake.clientsession.closed = True

    async def go():
        await s.open()
        new = s._spond
        await s.close()
        return new

    new = asyncio.run(go())
    assert new is not fake and type(new).__name__ == "Spond"


def test_export_rejects_non_xlsx(tmp_path, auth, clock, monkeypatch):
    s = _session(tmp_path, auth, clock)

    async def fake_core(*a, **kw):
        return b'{"error":"forbidden"}'

    monkeypatch.setattr(s, "_core", fake_core)
    with pytest.raises(SpondError, match="ikke en xlsx"):
        asyncio.run(s.export_xlsx("EV"))


def test_auth_status_never_shows_tokens(tmp_path, auth, clock):
    s = _session(tmp_path, auth, clock)
    asyncio.run(s._call(_ok(), "x"))
    st = s.auth_status()
    assert st["source"] == "login" and st["refresh_expires_days"] == 90.0
    assert "A1" not in str(st) and "R1" not in str(st)


# --------------------------------------------------------------------------- #
# Parsing og lås
# --------------------------------------------------------------------------- #
def test_parse_tokens_iso_and_epoch_ms():
    t = parse_tokens({"accessToken": {"token": "a", "expiration": "2027-01-01T00:00:00Z"},
                      "refreshToken": {"token": "r", "expiration": 1_900_000_000_000}},
                     now=T0, source="login")
    assert t.access_exp == 1798761600.0 and t.refresh_exp == 1_900_000_000.0


def test_parse_tokens_two_factor_and_lockout():
    with pytest.raises(TwoFactorRequired):
        parse_tokens({"token": "x", "phoneNumber": "+47"}, now=T0, source="login")
    with pytest.raises(AccountLocked):
        parse_tokens({"errorKey": "outOfLoginAttempts"}, now=T0, source="login")


def test_store_lock_is_exclusive(tmp_path):
    store = TokenStore(tmp_path / "token.json")

    async def go():
        async with store.locked():
            with pytest.raises(TimeoutError):
                async with store.locked(timeout=0.2):
                    pass

    asyncio.run(go())
