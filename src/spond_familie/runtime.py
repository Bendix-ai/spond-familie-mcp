"""Én delt Spond-økt per MCP-prosess, med lås og korttidsmellomlagring.

Transportnøytral: både stdio-serveren og ``--cli`` går gjennom ``Runtime.run``.
Låsen serialiserer verktøykallene, så to parallelle kall fra Claude ikke
logger inn samtidig og 0,7 s-pausen mellom Spond-kall faktisk virker.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._base import Settings
from .safety import PreviewStore
from .serialize import Directory, build_directory, _parse_iso
from .session import SpondSession
from .tokens import TokenStore

TTL_PROFILE = 24 * 3600
TTL_DIRECTORY = 15 * 60
TTL_EVENTS = 3 * 60
TTL_POSTS = 2 * 60
TTL_CHATS = 60


class Runtime:
    def __init__(self, *, config: Settings | None = None, session_factory=None,
                 clock=time.monotonic, now=None, mode: str = "local") -> None:
        """``mode="local"``: stdio på egen maskin; passord fra miljøet brukes som reserve
        hvis det er satt. ``mode="remote"``: nett-eksponert HTTP, kun refresh-token."""
        if mode not in ("local", "remote"):
            raise ValueError("mode må være local eller remote")
        self.mode = mode
        self._config = config
        self._session_factory = session_factory or (
            lambda cfg: SpondSession(cfg, store=TokenStore(), allow_password=(mode == "local"))
        )
        self._session = None
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock: asyncio.Lock | None = None
        self.previews = PreviewStore(clock=clock)
        self.errors: deque[str] = deque(maxlen=5)

    # ------------------------------------------------------------------ #
    @property
    def readonly(self) -> bool:
        return os.environ.get("SPOND_MCP_READONLY") == "1"

    @property
    def config(self) -> Settings:
        if self._config is None:
            self._config = Settings.from_env() if self.mode == "local" else Settings()
        return self._config

    def now(self) -> datetime:
        return self._now()

    async def session(self) -> SpondSession:
        if self._session is None:
            self._session = self._session_factory(self.config)
        return self._session

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run(self, fn, /, **kwargs):
        """Kjør ett verktøy under låsen; husk feilen til ``status``."""
        async with self.lock:
            try:
                return await fn(self, **kwargs)
            except Exception as exc:
                self.errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}"[:300])
                raise

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    # Mellomlagring
    # ------------------------------------------------------------------ #
    async def _cached(self, key: tuple, ttl: float, factory):
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = await factory()
        self._cache[key] = (now + ttl, value)
        return value

    def invalidate(self, *prefix) -> None:
        for key in [k for k in self._cache if k[: len(prefix)] == prefix]:
            del self._cache[key]

    @property
    def cache_size(self) -> int:
        now = self._clock()
        return sum(1 for exp, _ in self._cache.values() if exp > now)

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    async def directory(self) -> Directory:
        s = await self.session()

        async def build():
            profile = await self._cached(("profile",), TTL_PROFILE, s.fetch_profile)
            groups = await s.fetch_groups()
            return build_directory(profile, groups)

        return await self._cached(("directory",), TTL_DIRECTORY, build)

    async def events(self, group_id: str, start: datetime, end: datetime) -> list[dict]:
        """Arrangementer med start i ``[start, end)``, sortert.

        Biblioteket runder ``min_start``/``max_start`` ned til midnatt UTC. Vi
        henter et dag-justert vindu med margin (stabil cache-nøkkel gjennom
        døgnet) og filtrerer presist selv.
        """
        s = await self.session()
        fs = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=1)
        fe = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=2)
        raw = await self._cached(
            ("events", group_id, fs, fe), TTL_EVENTS,
            lambda: s.fetch_events_window(group_id, fs, fe),
        )
        out = []
        for e in raw:
            dt = _parse_iso(e.get("startTimestamp"))
            if dt is not None and start <= dt < end:
                out.append(e)
        out.sort(key=lambda e: e.get("startTimestamp") or "")
        return out

    async def posts(self, group_id: str) -> list[dict]:
        s = await self.session()
        return await self._cached(
            ("posts", group_id), TTL_POSTS,
            lambda: s.fetch_group_posts(group_id, max_posts=50, include_comments=True),
        )

    async def chats(self) -> list[dict]:
        s = await self.session()
        return await self._cached(("chats",), TTL_CHATS, lambda: s.fetch_chats(max_chats=50))
