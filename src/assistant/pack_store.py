"""Where the bot finds an app's pack, and the bot owner's ceilings over every pack.

Two kinds of app:
- a local pack: a YAML file in `apps/` (the demo, tests, and a fallback while an app moves to serving its own);
- a registered app: listed in `apps/registry.yaml` (or the `APPS` environment variable) with its site and the name of
  its secret's environment variable. The app serves its pack at `{site}/.well-known/app-assistant.json`.

A registered app's pack is fetched when first needed and kept in memory for 5 minutes; each good copy is also kept in
Postgres (`remote_packs`), so a cold instance still has it while the app's pack route is down. With no copy ever
fetched, a local YAML pack with the same id stands in; with neither, the app is unavailable (`get` answers None).

Every pack passes `clamp_pack`: an app can never raise what the owner allows by editing its own pack.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from assistant.config import Settings
from assistant.packs import AppPack, PackError, RegistryEntry, ServedPack

__all__ = ["PackStore", "RegistryEntry", "clamp_pack", "load_registry", "spend_key"]

log = logging.getLogger("assistant.packs")

WELL_KNOWN = "/.well-known/app-assistant.json"
CACHE_SECONDS = 300  # a fetched pack is used for this long before it is fetched again
RETRY_SECONDS = 60  # after a failed fetch (a stored copy, the YAML or nothing used meanwhile), try again after this
FETCH_SECONDS = 5  # the pack is fetched inside a chat request: never wait long for it
MAX_PACK_BYTES = 256 * 1024


def load_registry(path: Path, override: list[RegistryEntry] | None) -> list[RegistryEntry]:
    """The apps the bot trusts to serve their own pack: `override` (the APPS setting) when set, else the file's
    `apps:` list (none when the file is missing)."""
    if override is not None:
        entries = list(override)
        where = "APPS"
    elif not path.is_file():
        return []
    else:
        where = str(path)
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise PackError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("apps", []), list):
            raise PackError(f"{path}: the registry must be a mapping with an `apps:` list")
        try:
            entries = [RegistryEntry.model_validate(e) for e in raw.get("apps") or []]
        except ValidationError as exc:
            raise PackError(f"{path}: invalid registry entry: {exc}") from exc
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise PackError(f"{where}: duplicate app id {entry.id!r} in the registry")
        seen.add(entry.id)
    return entries


def clamp_pack(pack: AppPack, settings: Settings) -> AppPack:
    """The pack within the owner's ceilings: `message_usd_cap` capped at MAX_MESSAGE_USD (logged), and every model it
    names in ALLOWED_MODELS when that is set (PackError naming the others: such a pack is refused, never trimmed)."""
    allowed = settings.allowed_models
    if allowed is not None:
        m = pack.models
        named = [m.chat, m.writer, m.main, m.cheap, m.vision, *m.fallbacks]
        outside = [name for name in dict.fromkeys(named) if name not in allowed]
        if outside:
            raise PackError(f"pack {pack.id!r} names models outside ALLOWED_MODELS: {', '.join(outside)}")
    if pack.message_usd_cap > settings.max_message_usd:
        log.warning(
            "pack app=%s message_usd_cap=%s clamped to MAX_MESSAGE_USD=%s",
            pack.id,
            pack.message_usd_cap,
            settings.max_message_usd,
        )
        pack = pack.model_copy(update={"message_usd_cap": settings.max_message_usd})
    return pack


def spend_key(app: str, now: datetime | None = None) -> str:
    """The `counters` key of one app's model spend on one UTC day (MAX_APP_DAILY_USD)."""
    return f"spend:{app}:{(now or datetime.now(UTC)):%Y-%m-%d}"


class PackStore:
    """Every pack the bot serves, local and remote. `get` is what requests use."""

    def __init__(
        self,
        local: dict[str, AppPack],
        settings: Settings,
        registry: Iterable[RegistryEntry] = (),
        pool: AsyncConnectionPool | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._local = {app_id: clamp_pack(pack, settings) for app_id, pack in local.items()}
        self._registry = {entry.id: entry for entry in registry}
        self._pool = pool  # a registry needs both: the stored copies and the fetches
        self._http = http
        self._clock = clock
        self._cache: dict[str, tuple[AppPack | None, float]] = {}  # app id -> (pack or None, used until)
        self._locks: dict[str, asyncio.Lock] = {}

    def __getitem__(self, app_id: str) -> AppPack:
        """A local (YAML) pack, for startup checks and tests. Requests use `get`."""
        return self._local[app_id]

    def __setitem__(self, app_id: str, pack: AppPack) -> None:
        self._local[app_id] = clamp_pack(pack, self._settings)

    def entry(self, app_id: str) -> RegistryEntry | None:
        """The registry's entry for a registered app: enough to check its tokens even while its pack is unavailable."""
        return self._registry.get(app_id)

    def origins(self) -> list[str]:
        """Every local pack's allowed origins and each registered app's site origin (for CORS, set at startup)."""
        found = [o for pack in self._local.values() for o in pack.allowed_origins]
        found += [entry.origin for entry in self._registry.values()]
        return list(dict.fromkeys(found))

    async def get(self, app_id: str) -> AppPack | None:
        entry = self._registry.get(app_id)
        if entry is None:
            return self._local.get(app_id)
        if (cached := self._fresh(app_id)) is not None:
            return cached[0]
        async with self._locks.setdefault(app_id, asyncio.Lock()):  # one fetch at a time per app
            if (cached := self._fresh(app_id)) is not None:
                return cached[0]
            pack, seconds = await self._load(entry)
            self._cache[app_id] = (pack, self._clock() + seconds)
            return pack

    def _fresh(self, app_id: str) -> tuple[AppPack | None, float] | None:
        cached = self._cache.get(app_id)
        return cached if cached is not None and self._clock() < cached[1] else None

    async def _load(self, entry: RegistryEntry) -> tuple[AppPack | None, float]:
        """The app's pack and how long to use it: fetched, else the stored copy, else the YAML, else None."""
        try:
            raw = await self._fetch(entry)
            pack = self._accept(entry, raw)
        except PackError as e:
            log.warning("pack app=%s not used from %s%s: %s", entry.id, entry.site, WELL_KNOWN, e)
        else:
            await self._keep(entry.id, raw)
            return pack, CACHE_SECONDS
        stored = await self._stored(entry.id)
        if stored is not None:
            try:
                return self._accept(entry, stored), RETRY_SECONDS
            except PackError as e:  # e.g. ALLOWED_MODELS changed since it was kept
                log.warning("pack app=%s stored copy not used: %s", entry.id, e)
                return None, RETRY_SECONDS
        local = self._local.get(entry.id)  # only while no copy was ever fetched
        if local is None:
            log.warning("pack app=%s unavailable: no stored copy and no local pack", entry.id)
        return local, RETRY_SECONDS

    async def _fetch(self, entry: RegistryEntry) -> dict:
        assert self._http is not None
        url = entry.site + WELL_KNOWN
        try:
            async with self._http.stream(
                "GET", url, timeout=FETCH_SECONDS, follow_redirects=False, headers={"accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise PackError(f"the app answered {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PACK_BYTES:
                        raise PackError(f"the pack is over {MAX_PACK_BYTES // 1024} KB")
        except httpx.HTTPError as e:
            raise PackError(f"could not be fetched: {type(e).__name__}") from e
        try:
            raw = json.loads(bytes(body))
        except ValueError as e:
            raise PackError("the pack is not JSON") from e
        if not isinstance(raw, dict):
            raise PackError("the pack is not a JSON object")
        return raw

    def _accept(self, entry: RegistryEntry, raw: dict) -> AppPack:
        """A served pack checked against the contract and the registry, within the owner's ceilings."""
        try:
            served = ServedPack.model_validate(raw)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "the pack"
            raise PackError(f"not a valid pack ({e.error_count()} errors; {where}: {first['msg']})") from None
        if served.id != entry.id:
            raise PackError(f"the pack's id is {served.id!r}, not {entry.id!r}")
        try:
            return clamp_pack(served.app_pack(entry.secret_env, entry.site), self._settings)
        except ValidationError as e:  # the served shape is narrower than AppPack's: not expected
            raise PackError(f"not a valid pack ({e.error_count()} errors)") from None

    async def _keep(self, app_id: str, raw: dict) -> None:
        """Stores the last good copy as the app served it (ceilings are applied again when it is read)."""
        assert self._pool is not None
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "insert into remote_packs (app, pack, fetched_at) values (%s, %s, now())"
                    " on conflict (app) do update set pack = excluded.pack, fetched_at = excluded.fetched_at",
                    (app_id, Jsonb(raw)),
                )
        except Exception as e:  # noqa: BLE001 - the fetched pack is still used; only the copy is missed
            log.error("pack app=%s not stored: %s", app_id, type(e).__name__)

    async def _stored(self, app_id: str) -> dict | None:
        assert self._pool is not None
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("select pack from remote_packs where app = %s", (app_id,))
                row = await cur.fetchone()
        except Exception as e:  # noqa: BLE001 - told as no copy; logged by kind
            log.error("pack app=%s stored copy not read: %s", app_id, type(e).__name__)
            return None
        return row[0] if row is not None and isinstance(row[0], dict) else None
