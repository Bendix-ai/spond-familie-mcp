"""Delt lager for Spond-tokener på maskinen: ``~/.local/state/spond-familie/token.json``.

Alle MCP-prosesser (stdio fra Claude Code/Desktop og HTTP-tjenesten) deler
samme token-familie. Spond roterer refresh-tokenet ved hver fornying, og et
brukt refresh-token er dødt med én gang (målt 11.10.2026, ingen nådeperiode).
Derfor skjer fornying alltid under en fillås, etter å ha lest disken på nytt:
har en annen prosess allerede fornyet, tas det tokenet i bruk i stedet.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .safety import state_home

try:  # macOS/Linux
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class SpondTokens:
    access: str
    access_exp: float   # epoch-sekunder
    refresh: str
    refresh_exp: float  # epoch-sekunder
    obtained_at: float
    source: str         # "login" | "refresh" | "2fa"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SpondTokens":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def _write_private(path: Path, payload: dict) -> None:
    """Atomisk skriving med 0600: temp-fil, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class TokenStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else state_home() / "token.json"
        self._lock_path = self.path.with_name(self.path.stem + ".lock")
        self._block_path = self.path.with_name("login-block.json")

    def read(self) -> SpondTokens | None:
        data = _read_json(self.path)
        try:
            return SpondTokens.from_dict(data) if data else None
        except (KeyError, TypeError):
            return None

    def write(self, tokens: SpondTokens) -> None:
        _write_private(self.path, tokens.to_dict())

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    # Stoppbryter for passordinnlogging, delt mellom prosesser.
    def blocked_until(self) -> float | None:
        data = _read_json(self._block_path) or {}
        return data.get("until")

    def block(self, until: float) -> None:
        _write_private(self._block_path, {"until": until})

    @asynccontextmanager
    async def locked(self, timeout: float = 20.0):
        """Eksklusiv fillås (egen .lock-fil; os.replace bytter inode på token.json)."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            locked = False
            while not (locked := _try_lock(fd)):
                if time.monotonic() > deadline:
                    raise TimeoutError("Fikk ikke låst Spond-token-lageret")
                await asyncio.sleep(0.05)
            yield
        finally:
            try:
                if locked:
                    _unlock(fd)
            finally:
                os.close(fd)
