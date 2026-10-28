"""Invented catalogues through authenticated acquisition and immutable publication."""
from contextlib import contextmanager
import copy
import hashlib
import json

import pytest

from app.analytics.nlp_findings_store import FrozenFindings, checked_snapshot
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.nlp_read_projection import frozen_labels
from app.analytics.nlp_recovery import _resolver as recovery_resolver
from app.nlp.presentation import catalogue_resolver, load_resolver, MAX_CATALOG_BYTES
from app.nlp.release_catalog import canonical as canonical_bytes
from app.persistence.private_files import PrivateFileSecurityError
from tests.continuous_analytics_fixture import ACCOUNT, cleanup
from tests.test_nlp_artifact_store import example, profile_from, store, streams
from tests.test_nlp_dependencies import Environment

pytestmark = pytest.mark.ci_tier("integration")


def catalogue(profile, name="Example caf\u00e9 \U0001f600"):
    return {"policy_id": profile.presentation.policy_id,
            "revision": profile.presentation.revision,
            "labels": [{key: row[key] for key in ("task_contract", "taxonomy", "category_id")}
                       | {"display_label": name} for row in frozen_labels(profile)]}


def presentation_example(kind="split", *, name="Example caf\u00e9 \U0001f600", mutate=None):
    profile, payloads = example(kind)
    document = profile.model_dump(mode="json")
    value = catalogue(profile, name)
    if mutate is not None:
        mutate(value)
    raw = canonical_bytes(value)
    document["presentation"]["sha256"] = hashlib.sha256(raw).hexdigest()
    document["artifacts"].append({"artifact_id": "presentation", "role": "configuration",
        "format_id": "nlp-presentation.v1", "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()})
    payloads["presentation"] = raw
    for index, component in enumerate(document["components"]):
        topic = component["tasks"][0]["task"] == "message_topics"
        schema = "instruction-topic-config.v2" if topic else "boundary-entity-config.v2"
        if topic:
            config = dict(schema=schema, model="weights.topics", system_prompt="system",
                grammar="grammar", runtime={}, labels=[], policies={}, context_tokens=8192,
                output_tokens=192, inference_threads=2, presentation_catalog="presentation")
        else:
            config = dict(schema=schema, graphs={}, tokenizer="tokenizer", labels=[], policies={},
                          decoding={}, presentation_catalog="presentation")
        config_id = "config." + str(index)
        config_raw = canonical_bytes(config)
        document["artifacts"].append({"artifact_id": config_id, "role": "configuration",
            "format_id": schema, "size_bytes": len(config_raw),
            "sha256": hashlib.sha256(config_raw).hexdigest()})
        component["artifact_ids"].extend([config_id, "presentation"])
        payloads[config_id] = config_raw
    members = sorted((a for a in document["artifacts"] if a["role"] in
                      ("configuration", "weights", "tokenizer")), key=lambda a: a["artifact_id"])
    document["resources"]["temporary_bytes"] = 2 * sum(a["size_bytes"] for a in members) + len(canonical_bytes(members))
    return profile_from(document), payloads


@contextmanager
def environment(tmp_path, **kwargs):
    value = Environment(tmp_path, "split", profile_factory=lambda kind: presentation_example(kind, **kwargs))
    try:
        yield value
    finally:
        assert value.service.close()
        cleanup(value.fixture)


def publication(value):
    # No inference is needed to publish the current pending inventory.
    return FindingsRunner(value.fixture.source, value.fixture.stores.projections,
        authorize=lambda _: None, execution_ready=value.service.ready, dependencies=value.service, adapter=None,
        clock=lambda: value.fixture.clock.now, enrichment=value.fixture.pipeline.enrichment)


def saved(value, result):
    generation = value.fixture.stores.database.active_generation(ACCOUNT).generation_id
    with value.fixture.stores.database.read() as db:
        row = db.execute("SELECT * FROM nlp_generation_refs WHERE generation_id=?", (generation,)).fetchone()
        binding = FrozenFindings(row["account_ref"], row["snapshot_id"])
        return binding, checked_snapshot(db, binding)


def test_signed_acquisition_freeze_saved_read_and_recovery_need_no_optional_files(tmp_path, monkeypatch):
    with environment(tmp_path) as value:
        operation = value.acquire()
        assert value.service._operation(operation)["state"] == "complete", value.service._operation(operation)
        value.service.select(ACCOUNT, value.profile.profile_id, value.profile.revision)
        profile = value.profile
        assert len([a for a in profile.artifacts if a.format_id == "nlp-presentation.v1"]) == 1
        assert all("presentation" in c.artifact_ids for c in profile.components)
        available = value.artifacts.reopen(profile)
        catalog_asset = next(a for a in profile.artifacts if a.artifact_id == "presentation")
        assert "data/" + catalog_asset.sha256 in value.requests
        assert available.size_bytes >= sum(a.size_bytes for a in profile.artifacts
                                           if a.role in ("configuration", "weights", "tokenizer"))
        reads = []
        original = value.artifacts.read_configuration
        def counted(profile, artifact_id, **kwargs):
            reads.append(artifact_id)
            return original(profile, artifact_id, **kwargs)
        monkeypatch.setattr(value.artifacts, "read_configuration", counted)
        runner = publication(value)
        result = runner.publish_pending(ACCOUNT)
        assert result.changed
        assert sorted(reads) == ["config.0", "config.1", "presentation"]
        binding, snapshot = saved(value, result)
        labels = snapshot["read_inventory"]["labels"]
        assert len(labels) == 2
        assert {row["display_label"] for row in labels} == {"Example caf\u00e9 \U0001f600"}
        assert all(row["label_status"] == "resolved" for row in labels)
        assert all(row["presentation"] == profile.presentation.model_dump(mode="json") for row in labels)
        assert value.engine.prepares == len(profile.components)  # Acquisition warm-up only.
        # Make the entire optional closure and transport unavailable while preserving it.
        held = available.directory.with_name("held-catalogue")
        available.directory.rename(held)
        value.objects.clear()
        value.engine.present = False
        with value.fixture.stores.database.read() as db:
            assert checked_snapshot(db, binding) == snapshot
        recovered = recovery_resolver(profile, canonical_bytes(labels))
        assert frozen_labels(profile, recovered) == labels
        # A different catalogue cannot mutate previously retained metadata.
        updated = catalogue(profile, "Changed name")
        updated_profile = profile_from({**profile.model_dump(mode="json"), "presentation": {
            **profile.presentation.model_dump(mode="json"),
            "sha256": hashlib.sha256(canonical_bytes(updated)).hexdigest()}})
        assert {row["display_label"] for row in frozen_labels(updated_profile,
            catalogue_resolver(updated_profile, canonical_bytes(updated)))} == {"Changed name"}
        with value.fixture.stores.database.read() as db:
            assert checked_snapshot(db, binding)["read_inventory"]["labels"] == labels


@pytest.mark.parametrize("missing", [False, True])
def test_declared_corrupt_catalogue_refuses_publication_without_id_fallback(tmp_path, missing):
    with environment(tmp_path) as value:
        value.selected()
        available = value.artifacts.reopen(value.profile)
        member = next(a for a in available.artifacts if a.artifact.artifact_id == "presentation")
        if missing:
            member.path.rename(member.path.with_name("held-catalogue.data"))
        else:
            member.path.write_bytes(b"x" * member.artifact.size_bytes)
        with pytest.raises((ValueError, OSError, PrivateFileSecurityError)):
            publication(value).publish_pending(ACCOUNT)
        assert value.fixture.stores.database.active_generation(ACCOUNT) is None


def test_authenticated_incomplete_catalogue_refuses_publication(tmp_path):
    with environment(tmp_path, mutate=lambda value: value["labels"].pop()) as value:
        value.selected()
        with pytest.raises(ValueError, match="nlp_presentation_membership"):
            publication(value).publish_pending(ACCOUNT)
        assert value.fixture.stores.database.active_generation(ACCOUNT) is None


def test_v1_without_catalogue_retains_truthful_id_only_without_reading_data(tmp_path):
    profile, _ = example("split")
    assert load_resolver(profile, lambda _: pytest.fail("legacy data must not be read")) is None
    assert all(row["label_status"] == "id_only" and row["display_label"] == row["category_id"]
               for row in frozen_labels(profile))


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "taxonomy", "task", "category",
                                    "policy", "revision", "self_hash", "entry_field", "wrong_type"])
def test_catalogue_requires_closed_complete_identity_and_unique_task_category(change):
    profile, _ = example("split")
    value = catalogue(profile)
    if change == "missing": value["labels"].pop()
    elif change == "duplicate": value["labels"].append(copy.deepcopy(value["labels"][0]))
    elif change == "extra": value["labels"].append({**value["labels"][0], "category_id": "extra"})
    elif change in ("taxonomy", "task", "category"):
        field = {"taxonomy": "taxonomy", "task": "task_contract", "category": "category_id"}[change]
        value["labels"][0][field] = {} if change == "taxonomy" else "other"
    elif change in ("policy", "revision"):
        value["policy_id" if change == "policy" else "revision"] = "other"
    elif change == "self_hash": value["sha256"] = "a" * 64
    elif change == "entry_field": value["labels"][0]["extra"] = None
    elif change == "wrong_type": value["labels"][0]["category_id"] = []
    raw = canonical_bytes(value)
    document = profile.model_dump(mode="json")
    document["presentation"]["sha256"] = hashlib.sha256(raw).hexdigest()
    profile = profile_from(document)
    with pytest.raises(ValueError):
        catalogue_resolver(profile, raw)


@pytest.mark.parametrize("name", ["", "x" * 257, "line\nfeed", "\x00", "\u200b", "\ud800"])
def test_invalid_display_names_are_refused(name):
    profile, _ = presentation_example(name=name)
    with pytest.raises(ValueError):
        catalogue_resolver(profile, canonical_bytes(catalogue(profile, name)))


@pytest.mark.parametrize("name", ["x" * 256, "caf\u00e9", "cafe\u0301", "\u6f22\u5b57 \U0001f600"])
def test_unicode_and_shared_display_names_preserve_exact_spelling(name):
    profile, payloads = presentation_example(name=name)
    labels = frozen_labels(profile, catalogue_resolver(profile, payloads["presentation"]))
    assert len(labels) == 2 and all(row["display_label"] == name for row in labels)


@pytest.mark.parametrize("change", ["hash", "noncanonical", "duplicate_key", "utf16", "oversize", "constant"])
def test_catalogue_bytes_are_bounded_duplicate_safe_and_canonical(change):
    profile, payloads = presentation_example()
    raw = payloads["presentation"]
    if change == "hash": raw = raw.replace(b"Example", b"Changed")
    elif change == "noncanonical": raw += b" "
    elif change == "duplicate_key": raw = raw.replace(b'{"labels":', b'{"labels":[],"labels":', 1)
    elif change == "utf16": raw = raw.decode().encode("utf-16")
    elif change == "oversize": raw = b" " * (MAX_CATALOG_BYTES + 1)
    elif change == "constant": raw = b'{"labels":NaN}'
    with pytest.raises(ValueError):
        catalogue_resolver(profile, raw)


@pytest.mark.parametrize("change", ["missing_reference", "null_reference", "extra_field", "wrong_role",
                                    "wrong_format", "wrong_hash", "wrong_size", "missing_bytes"])
def test_verified_loader_refuses_declared_invalid_data(tmp_path, change):
    profile, payloads = presentation_example()
    document = profile.model_dump(mode="json")
    asset = next(a for a in document["artifacts"] if a["artifact_id"] == "presentation")
    if change in ("missing_reference", "null_reference", "extra_field"):
        config = json.loads(payloads["config.0"])
        if change == "missing_reference": config.pop("presentation_catalog")
        elif change == "null_reference": config["presentation_catalog"] = None
        else: config["extra"] = None
        payloads["config.0"] = canonical_bytes(config)
    elif change == "wrong_role": asset["role"] = "weights"
    elif change == "wrong_format": asset["format_id"] = "other.v1"
    elif change == "wrong_hash": asset["sha256"] = "0" * 64
    elif change == "wrong_size": asset["size_bytes"] += 1
    elif change == "missing_bytes": payloads.pop("presentation")
    if change == "wrong_role":
        with pytest.raises(ValueError):
            profile_from(document)
        return
    profile = profile_from(document)
    def read(key):
        if key not in payloads:
            raise ValueError("missing_configuration")
        return payloads[key]
    with pytest.raises(ValueError):
        load_resolver(profile, read)


def test_configuration_leases_reject_tampered_manifest_and_enforce_closure_budget(tmp_path):
    profile, payloads = presentation_example()
    target = store(tmp_path)
    available = target.stage(profile, streams(payloads))
    assert target.read_configuration(profile, "presentation") == payloads["presentation"]
    from app.nlp.artifact_store import ArtifactStore, StoreLimits
    bounded = ArtifactStore(target.root, StoreLimits(1, 16 * 1024 * 1024, 0))
    with pytest.raises(ValueError, match="closure_quota"):
        bounded.read_configuration(profile, "presentation")
    (available.directory / "artifacts.json").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        target.read_configuration(profile, "presentation")


def test_declared_catalogue_requires_a_verified_data_owner():
    profile, _ = presentation_example()
    with pytest.raises(ValueError, match="nlp_presentation_unavailable"):
        load_resolver(profile)


def test_same_category_id_is_distinct_across_task_contracts():
    profile, _ = example("split")
    document = profile.model_dump(mode="json")
    for component in document["components"]:
        component["tasks"][0]["enabled_ids"] = ["category.shared"]
        component["executed_labels"][0]["label_id"] = "category.shared"
    profile = profile_from(document)
    value = catalogue(profile)
    raw = canonical_bytes(value)
    document["presentation"]["sha256"] = hashlib.sha256(raw).hexdigest()
    profile = profile_from(document)
    labels = frozen_labels(profile, catalogue_resolver(profile, raw))
    assert len(labels) == 2
    assert len({(row["task_contract"], row["category_id"]) for row in labels}) == 2
    assert {row["category_id"] for row in labels} == {"category.shared"}


def test_explicit_fixture_resolver_has_priority_over_catalogue_loading(tmp_path, monkeypatch):
    with environment(tmp_path) as value:
        value.selected()
        monkeypatch.setattr(value.service, "presentation_resolver",
                            lambda _: pytest.fail("explicit fixture resolver owns the labels"))
        runner = publication(value)
        runner.label_resolver = lambda identity: {**identity, "display_label": "Fixture name"}
        result = runner.publish_pending(ACCOUNT)
        _, snapshot = saved(value, result)
        assert {row["display_label"] for row in snapshot["read_inventory"]["labels"]} == {"Fixture name"}
