"""Real producer vectors, durable authority, and nonblocking service refusal."""
import asyncio
import copy
import hashlib
import io
import json
import tarfile
import threading
import base64
from pathlib import Path

import pytest

from app.protocol.signing_parameters import ParameterRequest
from app.security.signing_parameter_publications import (
    PublicationRejected, canonical, decode, verify_keyset, verify_catalog, verify_object,
    validate_trust, monotonic, checkpoint,
)
from app.services.signing_parameters import SigningParameterService, DistributionUnavailable

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.ci_tier("fast")]
ARCHIVE = ROOT / "extension/vendor/local-authenticated-read-connector-0.6.0-rc.4.tgz"


def vectors():
    raw = ARCHIVE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "6a9fb0404a26a745341e0a7c1bf12a8ae4f9260ca536fab778ce8f1995b7f6b7"
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        raw = archive.extractfile("package/publication/vectors.json").read()
    assert hashlib.sha256(raw).hexdigest() == "e539f44ce5765d7b0262aef08f362e9c87ed3b3ce6422a18bcaf287e357f109b"
    return json.loads(raw)


@pytest.mark.parametrize("fixture", vectors()["fixtures"], ids=lambda fixture: fixture["name"])
def test_independent_python_real_producer_vectors(fixture):
    try:
        keys = verify_keyset(fixture["keyset"], vectors()["trust"], now=fixture["now"])
        catalog = verify_catalog(fixture["catalog"], keys, now=fixture["now"])
        entry = next(entry for entry in catalog.payload["rules"] if entry["source_revision"] == fixture["source_revision"])
        verify_object(decode(fixture["object"], 16384), entry)
        accepted = True
    except PublicationRejected:
        accepted = False
    assert accepted == fixture["valid"]


def test_raw_object_integral_numeric_representations_match_packaged_js():
    fixture = json.loads((ROOT / "extension/tests/fixtures/signing-parameter-numeric-representation.json").read_bytes())
    keys = verify_keyset(fixture["keyset"], fixture["trust"], now=fixture["now"])
    catalog = verify_catalog(fixture["catalog"], keys, now=fixture["now"])
    raw = decode(fixture["object"], 16384)
    assert b"0.0" in raw and b"-17e0" in raw
    entry = catalog.payload["rules"][0]
    assert verify_object(raw, entry)["format"]["checksum_constant"] == -17


def test_production_pin_refuses_renamed_synthetic_root():
    trust = copy.deepcopy(vectors()["trust"])
    trust["roots"][0]["kid"] = "dipsy-signing-root-01"
    with pytest.raises(PublicationRejected):
        validate_trust(trust, production=True)


def test_sequence_conflict_uses_payload_not_signature():
    fixture = vectors()["fixtures"][0]
    keys = verify_keyset(fixture["keyset"], vectors()["trust"], now=fixture["now"])
    saved = checkpoint(keys)
    monotonic(saved, keys)
    with pytest.raises(PublicationRejected):
        monotonic({**saved, "sequence": saved["sequence"] + 1}, keys)
    with pytest.raises(PublicationRejected):
        monotonic({**saved, "payload_sha256": "0" * 64}, keys)


class MemoryPersistence:
    def __init__(self):
        self.value = None
        self.objects = {}
        self.block = None
        self.entered = threading.Event()
        self.damage = False
    def load(self):
        if self.damage:
            raise ValueError("damaged")
        return copy.deepcopy(self.value)
    def save(self, state):
        if self.block:
            self.entered.set()
            self.block.wait(3)
        self.value = copy.deepcopy(state)
    def read_object(self, entry):
        return self.objects.get(entry["sha256"])
    def promote(self, raw, entry, retained):
        self.objects[entry["sha256"]] = raw


class ProducerDistribution:
    def __init__(self):
        self.fixture = vectors()["fixtures"][0]
        self.calls = []
        self.block = asyncio.Event()
        self.block.set()
        self.failed = False
    async def read(self, path, maximum):
        self.calls.append(path)
        await self.block.wait()
        if self.failed:
            raise DistributionUnavailable()
        return (self.fixture["keyset"].encode() if path == "publisher-keyset.jws" else
                self.fixture["catalog"].encode() if path == "catalog.jws" else decode(self.fixture["object"], maximum))


def request():
    return ParameterRequest.model_validate({"schema": "signing-parameter-request/v1", "engine": "onlyfans-sha1-checksum/v1",
        "parameter_schema": "local-packaged-signing-rule/v1", "source_revision": "revision-1", "catalog_sequence": 0,
        "keyset_sequence": 0, "object_sha256": None})


def signed_catalog(payload):
    # Same canonical serialization and ES256 compact profile as real producer
    # vectors. Public scalar 2 is isolated conformance material only.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    def b64(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    message = b64(canonical({"alg": "ES256", "kid": "synthetic-publisher", "typ": "signing-parameter-catalog+jws"})) + "." + b64(canonical(payload))
    r, s = utils.decode_dss_signature(ec.derive_private_key(2, ec.SECP256R1()).sign(message.encode(), ec.ECDSA(hashes.SHA256())))
    return message + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def test_saturated_obsolete_requests_retire_and_next_published_revision_progresses():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        clock = [distribution.fixture["now"]]
        distribution.block.clear()
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: clock[0])
        await service.start()
        for index in range(128):
            service.schedule(f"obsolete-{index}")
        service.schedule("revision-1")
        assert len(service.needed) == 128
        distribution.block.set(); await service.task
        assert service.needed == set()
        clock[0] += 31
        assert (await service.resolve(request()))["status"] == "pending"
        await service.task
        assert (await service.resolve(request()))["status"] == "available"
        assert "objects/sha256/" in distribution.calls[-1]
        await service.close()
    asyncio.run(run())


def test_metadata_retirement_does_not_drop_requests_arriving_during_promotion():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        entered, release = threading.Event(), threading.Event()
        promote = persistence.promote
        def blocked(*args):
            entered.set(); release.wait(3); promote(*args)
        persistence.promote = blocked
        service = SigningParameterService(persistence, vectors()["trust"], distribution,
                                          clock=lambda: distribution.fixture["now"])
        await service.start(); service.schedule("revision-1")
        assert await asyncio.to_thread(entered.wait, 2)
        service.schedule("late-arrival")
        release.set(); await service.task
        assert service.needed == {"late-arrival"}
        service.schedule(force=True); await service.task
        assert service.needed == set()
        assert (await service.resolve(request()))["status"] == "available"
        await service.close()
    asyncio.run(run())


def test_new_metadata_withdrawal_and_failed_replacement_are_durable_before_objects():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await service.start(); await service.resolve(request()); await service.task
        previous = copy.deepcopy(persistence.value)
        catalog = json.loads(decode(distribution.fixture["catalog"].split(".")[1], 32768))
        catalog.update(sequence=2, rules=[])
        distribution.fixture = {**distribution.fixture, "catalog": signed_catalog(catalog)}
        service.schedule(force=True); await service.task
        assert (await service.resolve(request()))["status"] == "not_published"
        assert persistence.value["catalog"]["sequence"] == 2
        assert service.objects == {}
        # A superseded same-sequence conflict cannot recover old usable bytes.
        distribution.fixture = {**distribution.fixture, "catalog": previous["catalog"]["jws"]}
        service.schedule(force=True); await service.task
        assert service.failure == "verification"
        assert (await service.resolve(request()))["status"] == "not_published"
        await service.close()
    asyncio.run(run())


def test_object_disk_failure_is_typed_storage_with_committed_metadata():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        persistence.promote = lambda *args: (_ for _ in ()).throw(OSError("isolated disk failure"))
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await service.start(); await service.resolve(request()); await service.task
        result = await service.resolve(request())
        assert result["status"] == "unavailable" and result["reason"] == "storage"
        assert persistence.value["catalog"]["sequence"] == 1
        assert result["catalog"] == distribution.fixture["catalog"]
        await service.close()
    asyncio.run(run())


def test_shutdown_drains_entered_object_promotion_before_replacement_owner():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        entered, release = threading.Event(), threading.Event()
        promote = persistence.promote
        def blocked(*args):
            entered.set(); release.wait(3); promote(*args)
        persistence.promote = blocked
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await service.start(); await service.resolve(request())
        assert await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set(); await closing
        persistence.promote = promote
        restarted = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await restarted.start()
        assert (await restarted.resolve(request()))["status"] == "available"
        await restarted.close()
    asyncio.run(run())


def test_rpc_is_nonblocking_coalesces_and_drains_clock_save():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        clock = [distribution.fixture["now"]]
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: clock[0])
        await service.start()
        distribution.block.clear()
        results = await asyncio.wait_for(asyncio.gather(*(service.resolve(request()) for _ in range(20))), 0.2)
        assert all(result["status"] == "pending" for result in results)
        distribution.block.set()
        await service.task
        assert len(distribution.calls) == 3
        assert (await service.resolve(request()))["status"] == "available"
        clock[0] += 1
        persistence.block = threading.Event()
        assert (await asyncio.wait_for(service.resolve(request()), 0.2))["status"] == "pending"
        assert (await asyncio.wait_for(service.resolve(request()), 0.2))["status"] == "pending"
        assert await asyncio.wait_for(asyncio.to_thread(persistence.entered.wait, 1), 1.2)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not closing.done()
        persistence.block.set()
        await closing
        assert persistence.value["clock_high_water"] == clock[0]
    asyncio.run(run())


def test_restart_cache_deletion_preserves_authority_and_damage_refuses():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await service.start()
        await service.resolve(request())
        await service.task
        await service.close()
        saved = copy.deepcopy(persistence.value)
        persistence.objects.clear()
        restarted = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await restarted.start()
        assert restarted.state["catalog"] == saved["catalog"]
        assert (await restarted.resolve(request()))["status"] == "pending"
        await restarted.close()
        persistence.damage = True
        failed = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: distribution.fixture["now"])
        await failed.start()
        assert failed.task is None
        assert (await failed.resolve(request()))["reason"] == "storage"
        await failed.close()
    asyncio.run(run())


def test_advancing_wall_clock_retries_reach_available_and_commit_each_decision():
    async def run():
        persistence, distribution = MemoryPersistence(), ProducerDistribution()
        clock = [distribution.fixture["now"]]
        service = SigningParameterService(persistence, vectors()["trust"], distribution, clock=lambda: clock[0])
        await service.start()
        await service.resolve(request())
        await service.task
        for _ in range(4):
            clock[0] += 1
            result = await asyncio.wait_for(service.resolve(request()), 0.2)
            assert result["status"] == "available"
            assert persistence.value["clock_high_water"] >= clock[0]
        await service.close()
    asyncio.run(run())


def test_auth_store_checkpoint_row_is_authoritative(tmp_path):
    from app.persistence.auth import SQLiteAuthenticationStore
    from app.persistence.signing_parameters import SigningParameterPersistence
    authentication = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    store = SigningParameterPersistence(authentication, tmp_path / "cache")
    assert store.load() is None
    value = {"schema": "fixture", "clock_high_water": 10}
    store.save(value)
    assert store.load() == value
    with authentication.database.transaction() as connection:
        connection.execute("DELETE FROM signing_publication_authority")
    with pytest.raises(PublicationRejected):
        store.load()
