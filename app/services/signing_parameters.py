"""Application-owned public parameter service; RPC never waits for HTTP.

One background task coalesces work. Publication authority is durable before
object promotion, independent of authentication tickets and account state.
"""
from __future__ import annotations

import asyncio
import copy
import json
import random
import time
from pathlib import Path

import httpx

from app.core.resource_paths import resource_path
from app.security.signing_parameter_publications import (
    CATALOG_LIMIT, KEYSET_LIMIT, OBJECT_LIMIT, ENGINE, PARAMETER_SCHEMA,
    PRODUCTION_TRUST_SHA256, PublicationRejected, canonical, sha256, closed,
    integer, require, checkpoint, check_checkpoint, monotonic, encode,
    verify_keyset, verify_catalog, verify_object, validate_trust,
)

REFRESH_SECONDS = 6 * 3600
NEGATIVE_SECONDS = 30
FETCH_DEADLINE_SECONDS = 15
RETRY_MS = 30_000
MAX_NEEDED_REVISIONS = 128
CLOCK_COMMIT_RPC_SECONDS = 0.05


class DistributionUnavailable(RuntimeError):
    pass


class FixedDistribution:
    """Only release-bound paths; redirects, credentials and runtime overrides refuse."""
    def __init__(self):
        release = json.loads(resource_path("app/core/signing-parameter-release.json").read_text(encoding="utf-8"))
        closed(release, ("schema", "origin", "base_path", "trust_sha256"))
        require(release == {"schema": "ofca-signing-parameter-release/v1", "origin": "https://updates.dipsy.fi",
                            "base_path": "/signing-parameters/v1/", "trust_sha256": PRODUCTION_TRUST_SHA256})
        self.base = release["origin"] + release["base_path"]

    async def read(self, relative, maximum):
        import re
        require(relative in {"publisher-keyset.jws", "catalog.jws"}
                or re.fullmatch(r"objects/sha256/[a-f0-9]{64}\.json", relative) is not None)
        # aiter_bytes bounds decoded bytes even if the server uses compression.
        try:
            async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
                async with httpx.AsyncClient(follow_redirects=False, timeout=10, trust_env=False) as client:
                    async with client.stream("GET", self.base + relative, headers={"Accept": "application/json, application/jose"}) as response:
                        response.raise_for_status()
                        require(response.status_code == 200)
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            require(len(raw) + len(block) <= maximum)
                            raw.extend(block)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise DistributionUnavailable() from None
        return bytes(raw)


class SigningParameterService:
    def __init__(self, persistence, trust, distribution, *, clock=time.time, jitter=random.uniform):
        self.persistence, self.trust, self.distribution = persistence, validate_trust(copy.deepcopy(trust)), distribution
        self.clock, self.jitter = clock, jitter
        self.state = None
        self.keyset = self.catalog = None
        self.objects = {}
        self.needed = set()
        self.last_fetch = 0
        self.failure = None
        self.task = None
        self.periodic = None
        self.clock_task = None
        self.clock_target = 0
        self.closed = False
        self.lock = asyncio.Lock()

    def _now(self):
        now = int(self.clock())
        require(integer(now))
        high = 0 if self.state is None else self.state["clock_high_water"]
        require(now + 300 >= high)
        return max(now, high)

    async def start(self):
        # Disk/verification only. Startup neither triggers nor waits for a HTTP
        # response; activation and hello remain independently ready.
        try:
            state = await asyncio.to_thread(self.persistence.load)
            now = int(self.clock())
            if state is None:
                state = {"schema": "signing-parameter-brain-state/v1", "trust_sha256": sha256(canonical(self.trust)),
                         "clock_high_water": now, "keyset": None, "catalog": None}
                await asyncio.to_thread(self.persistence.save, state)
            closed(state, ("schema", "trust_sha256", "clock_high_water", "keyset", "catalog"))
            require(state["schema"] == "signing-parameter-brain-state/v1"
                    and state["trust_sha256"] == sha256(canonical(self.trust)) and integer(state["clock_high_water"])
                    and now + 300 >= state["clock_high_water"])
            now = max(now, state["clock_high_water"])
            if state["keyset"] is not None:
                self.keyset = verify_keyset(state["keyset"]["jws"], self.trust, now=now, allow_expired=True)
                check_checkpoint(state["keyset"], self.keyset)
            if state["catalog"] is not None:
                saved = closed(state["catalog"], ("jws", "sequence", "payload_sha256", "keyset_jws"))
                old_keys = verify_keyset(saved["keyset_jws"], self.trust, now=now, allow_expired=True)
                self.catalog = verify_catalog(saved["jws"], old_keys, now=now, allow_expired=True)
                check_checkpoint({k: v for k, v in saved.items() if k != "keyset_jws"}, self.catalog)
                require(self.keyset is not None and old_keys.payload["sequence"] <= self.keyset.payload["sequence"])
                require(old_keys.payload["sequence"] != self.keyset.payload["sequence"] or old_keys.payload_sha256 == self.keyset.payload_sha256)
            self.state = state
            if self.catalog:
                for entry in self.catalog.payload["rules"]:
                    try:
                        raw = await asyncio.to_thread(self.persistence.read_object, entry)
                        if raw is not None:
                            verify_object(raw, entry)
                            self.objects[entry["sha256"]] = raw
                    except Exception:
                        # Only disposable bytes are missing/damaged; checkpoints stay.
                        continue
        except Exception:
            self.failure = "storage"
            self.state = None
        if self.state is not None:
            self.schedule()
            self.periodic = asyncio.create_task(self._periodic())

    async def _periodic(self):
        try:
            while not self.closed:
                await asyncio.sleep(REFRESH_SECONDS + self.jitter(-1800, 1800))
                self.schedule(force=True)
        except asyncio.CancelledError:
            pass

    def schedule(self, revision=None, *, force=False):
        if self.closed or self.state is None:
            return
        if revision is not None:
            if revision in self.needed or len(self.needed) < MAX_NEEDED_REVISIONS:
                self.needed.add(revision)
            # Saturation must not suppress metadata refresh. A newer catalog
            # can retire obsolete requests and admit this revision on retry.
        if self.task is not None or (not force and self.clock() < self.last_fetch + NEGATIVE_SECONDS):
            return
        self.task = asyncio.create_task(self._refresh())
        self.task.add_done_callback(self._completed)

    def _completed(self, task):
        if self.task is task:
            self.task = None
        # Observe failures without including publication or transport bytes.
        if not task.cancelled():
            task.exception()

    async def _refresh(self):
        self.last_fetch = self.clock()
        try:
            async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
                raw_keys = await self.distribution.read("publisher-keyset.jws", (KEYSET_LIMIT * 4 + 2) // 3 + 1024)
                keys = verify_keyset(raw_keys.decode("ascii"), self.trust, now=self._now())
                async with self.lock:
                    monotonic(self.state["keyset"], keys)
                    state = {**self.state, "clock_high_water": self._now(), "keyset": checkpoint(keys)}
                    await self._save(state)
                    self.keyset = keys
                raw_catalog = await self.distribution.read("catalog.jws", (CATALOG_LIMIT * 4 + 2) // 3 + 1024)
                catalog = verify_catalog(raw_catalog.decode("ascii"), keys, now=self._now())
                async with self.lock:
                    monotonic(self.state["catalog"], catalog)
                    state = {**self.state, "clock_high_water": self._now(), "catalog": {**checkpoint(catalog), "keyset_jws": keys.jws}}
                    await self._save(state)
                    self.catalog = catalog
                    retained = {entry["sha256"] for entry in catalog.payload["rules"]}
                    self.objects = {digest: raw for digest, raw in self.objects.items() if digest in retained}
                    requested = self.needed.copy()
                    mapped = {entry["source_revision"]: entry for entry in catalog.payload["rules"]}
                    self.needed.difference_update(revision for revision in requested
                        if revision not in mapped or mapped[revision]["sha256"] in self.objects)
                # A withdrawal above is already durable if any following object fails.
                for entry in catalog.payload["rules"]:
                    if entry["source_revision"] not in requested or entry["sha256"] in self.objects:
                        continue
                    raw = await self.distribution.read("objects/sha256/" + entry["sha256"] + ".json", OBJECT_LIMIT)
                    verify_object(raw, entry)
                    await self._disk_write(self.persistence.promote, raw, entry, retained)
                    if not self.closed:
                        self.objects[entry["sha256"]] = raw
                        self.needed.discard(entry["source_revision"])
                self.failure = None
        except asyncio.CancelledError:
            raise
        except (PublicationRejected, UnicodeError):
            self.failure = "verification"
        except (DistributionUnavailable, TimeoutError):
            self.failure = "network"
        except Exception:
            self.failure = "storage"

    async def _save(self, state):
        await self._disk_write(self.persistence.save, state)
        self.state = state

    async def _disk_write(self, work, *args):
        # Cancellation must drain an already-entered durable write.
        saving = asyncio.create_task(asyncio.to_thread(work, *args))
        try:
            await asyncio.shield(saving)
        except asyncio.CancelledError:
            await saving
            raise

    def _schedule_clock(self, now):
        self.clock_target = max(self.clock_target, now)
        if self.clock_task is None:
            self.clock_task = asyncio.create_task(self._commit_clock())
            self.clock_task.add_done_callback(lambda task: setattr(self, "clock_task", None))

    async def _commit_clock(self):
        try:
            async with self.lock:
                await self._save({**self.state, "clock_high_water": max(self.state["clock_high_water"], self.clock_target)})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = "storage"

    def _metadata(self):
        if self.keyset is None or self.catalog is None:
            return {"keyset": None, "catalog": None}
        return {"keyset": self.keyset.jws, "catalog": self.catalog.jws}

    def _eligible(self, now):
        if self.keyset is None or self.catalog is None:
            return False
        return now < min(self.keyset.payload["expires_at"], self.catalog.payload["expires_at"]) and any(
            key["kid"] == self.catalog.kid and self.catalog.payload["issued_at"] >= key["not_before"]
            and self.catalog.payload["expires_at"] <= key["not_after"]
            for key in self.keyset.payload["keys"])

    async def resolve(self, request):
        # Validated inner DTO only. No caller identity is retained or downloaded.
        revision = request.source_revision
        result = {"schema": "signing-parameter-resolution/v1", **self._metadata()}
        if self.closed or self.state is None:
            return {**result, "status": "unavailable", "reason": "storage", "retry_after_ms": RETRY_MS}
        try:
            now = self._now()
            self.schedule(revision)
            if now > self.state["clock_high_water"]:
                self._schedule_clock(now)
                # Bound the durable decision acknowledgement; never await HTTP
                # or cancel an entered write. A retry in the next wall-clock
                # second can complete its own snapshot rather than livelock.
                await asyncio.wait({self.clock_task}, timeout=CLOCK_COMMIT_RPC_SECONDS)
                if now > self.state["clock_high_water"]:
                    return {**result, "status": "pending", "retry_after_ms": 1000}
            result = {"schema": "signing-parameter-resolution/v1", **self._metadata()}
            if not self._eligible(now):
                reason = self.failure or ("freshness" if self.catalog else "network")
                return {**result, "status": "pending" if self.task else "unavailable", "retry_after_ms": RETRY_MS,
                        **({} if self.task else {"reason": reason})}
            catalog = self.catalog.payload
            if catalog["engine"] != ENGINE or catalog["parameter_schema"] != PARAMETER_SCHEMA:
                return {**result, "status": "unsupported_engine"}
            entry = next((entry for entry in catalog["rules"] if entry["source_revision"] == revision), None)
            if entry is None:
                return {**result, "status": "not_published", "retry_after_ms": RETRY_MS}
            raw = self.objects.get(entry["sha256"])
            if raw is None:
                return {**result, "status": "pending" if self.task else "unavailable", "retry_after_ms": RETRY_MS,
                        **({} if self.task else {"reason": self.failure or "network"})}
            verify_object(raw, entry)
            if request.object_sha256 == entry["sha256"] and request.catalog_sequence <= catalog["sequence"] and request.keyset_sequence <= self.keyset.payload["sequence"]:
                return {**result, "status": "not_modified", "sha256": entry["sha256"]}
            result = {**result, "status": "available", "object": encode(raw)}
            require(len(canonical(result)) < 256 * 1024)
            return result
        except PublicationRejected:
            return {**result, "status": "unavailable", "reason": "freshness", "retry_after_ms": RETRY_MS}
        except Exception:
            return {**result, "status": "unavailable", "reason": "storage", "retry_after_ms": RETRY_MS}

    async def close(self):
        self.closed = True
        tasks = [task for task in (self.periodic, self.task, self.clock_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_service = None


async def start_signing_parameters(authentication_store_factory):
    global _service
    from app.core.config import settings
    from app.persistence.signing_parameters import SigningParameterPersistence
    if _service is not None or settings.environment.lower() == "test":
        return
    trust = json.loads(resource_path("app/security/signing-publication-trust.json").read_text(encoding="utf-8"))
    validate_trust(trust, production=True)
    store = await asyncio.to_thread(authentication_store_factory)
    _service = SigningParameterService(SigningParameterPersistence(store, Path(settings.auth_database_path).parent / "signing-parameter-cache"), trust, FixedDistribution())
    await _service.start()


def signing_parameter_service():
    return _service


async def stop_signing_parameters():
    global _service
    if _service is not None:
        await _service.close()
        _service = None
