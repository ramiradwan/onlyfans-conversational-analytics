"""Offline signing binds exact prepared releases without admitting execution."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from app.nlp import profile_authentication as authentication
from app.nlp.release_catalog import canonical, verify_catalog, CATALOG_TYPE, ReleaseCatalog
from app.persistence.private_files import apply_private_file_security
from app.security import signed_objects
from tests.test_nlp_profile_authentication import keys, sign
from tests.test_nlp_profile_packaging import Example, pin
from tools import nlp_release_signing as signing

pytestmark = pytest.mark.ci_tier("fast")


class Release:
    def __init__(self, root, *, overbudget=False, version="nlp-profile.v3"):
        package_root = root / "prepared"
        package_root.mkdir()
        self.package = Example(package_root, version=version)
        if overbudget:
            self.package.document["resources"]["transition_peak_memory_bytes"] = 2 * 1024**3
            self.package.save()
        self.package.run()
        self.profile_sha = signing.packaging.digest((self.package.output / "profile.json").read_bytes())
        self.path = root / "signing-inputs.json"
        self.catalog_path = root / "catalog.json"
        self.trust_path = root / "trust.json"
        self.key_path = root / "key.pem"
        self.output = root / "signed"
        self.key = ec.derive_private_key(37, ec.SECP256R1())
        self.kid = "example-release-key"
        self.keys = keys(self.key, self.kid)
        self.trust_path.write_bytes(canonical(self.keys))
        raw = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
        with self.key_path.open("xb") as stream:
            apply_private_file_security(self.key_path)
            stream.write(raw)
        self.catalog = dict(schema_version="nlp-release-catalog.v1", sequence=1,
            issued_at=1, expires_at=1000, withdrawals=[],
            profiles=[json.loads((self.package.output / "release-entry.json").read_bytes())])
        self.inputs = dict(schema="nlp-release-signing-inputs/v1", packages=[self.package_pin()],
            previous_catalog=None, trusted_keys=pin(self.trust_path), signer=pin(self.key_path),
            kid=self.kid, maximum_output_bytes=1024**2, minimum_free_bytes=1024)
        self.save()

    def package_pin(self):
        return dict(directory=str(self.package.output), inventory=pin(self.package.output / "inventory.json"),
                    producer_receipt=pin(self.package.output / "producer-receipt.json"))

    def save(self):
        self.catalog_path.write_bytes(canonical(self.catalog))
        self.inputs["catalog"] = pin(self.catalog_path)
        self.write_inputs()

    def write_inputs(self):
        self.path.write_bytes(canonical(self.inputs))

    def run(self, **kwargs):
        return signing.prepare(self.path, pin(self.path)["sha256"], self.output, **kwargs)

    def prior(self, *, reuse=True):
        self.run()
        previous = self.output
        self.inputs["previous_catalog"] = pin(previous / "catalog.jws")
        if reuse:
            self.inputs["reuse_profiles"] = [dict(profile_sha256=self.profile_sha,
                envelope=pin(previous / "profiles" / self.profile_sha))]
        self.output = self.output.with_name("next")
        self.catalog.update(sequence=2, issued_at=2)
        self.save()
        return previous


@pytest.mark.parametrize("version", ["nlp-profile.v2", "nlp-profile.v3"])
def test_real_atomic_output_contains_valid_exact_envelopes_and_no_data_or_secrets(tmp_path, version):
    value = Release(tmp_path, version=version)
    before = {path: pin(path) for path in value.package.output.rglob("*") if path.is_file()}
    receipt = value.run()
    files = {path.relative_to(value.output).as_posix() for path in value.output.rglob("*") if path.is_file()}
    assert files == {"catalog.jws", "profiles/" + value.profile_sha, "publication-inventory.json", "producer-receipt.json"}
    catalog, _ = verify_catalog((value.output / "catalog.jws").read_bytes(), value.keys)
    assert catalog.model_dump(mode="json") == value.catalog
    raw = (value.output / "profiles" / value.profile_sha).read_bytes()
    authenticated = authentication.authenticate_profile(raw, trusted_nlp_keys=value.keys,
        expected_profile_sha256=value.profile_sha)
    assert authenticated.profile.schema_version == version
    assert canonical(authenticated.profile) == (value.package.output / "profile.json").read_bytes()
    assert receipt["approved"] is False and receipt["runtime_executed"] is False
    assert receipt["resource_admission"] == "not_evaluated"
    inventory = json.loads((value.output / "publication-inventory.json").read_bytes())
    assert {row["file"] for row in inventory["data"]} == {
        "data/" + a.sha256 for a in authenticated.profile.artifacts if a.role in signing.packaging.DATA_ROLES}
    for row in inventory["files"]:
        observed = pin(value.output / row["file"])
        assert (observed["size_bytes"], observed["sha256"]) == (row["size_bytes"], row["sha256"])
    assert all(b"PRIVATE KEY" not in path.read_bytes() and str(tmp_path).encode() not in path.read_bytes()
               for path in value.output.rglob("*") if path.is_file())
    assert {path: pin(path) for path in before} == before
    assert not list(tmp_path.glob(".*.stage-*"))


def test_validate_only_matches_public_key_but_never_signs_or_writes(tmp_path, monkeypatch):
    value = Release(tmp_path)
    monkeypatch.setattr(signing, "_sign", lambda *args: pytest.fail("validation signed"))
    result = value.run(validate_only=True)
    assert result["validation_only"] and result["signed"] is False
    assert not value.output.exists()


def test_signature_authenticity_does_not_raise_resource_policy(tmp_path):
    value = Release(tmp_path, overbudget=True)
    value.run()
    raw = (value.output / "profiles" / value.profile_sha).read_bytes()
    observed = signed_objects.verify_compact(raw.decode(), typ="nlp-profile.v3+jws", keys=value.keys,
        maximum=authentication.MAX_PROFILE_BYTES, envelope_limit=authentication.MAX_ENVELOPE_BYTES,
        header_canonicalizer=canonical, payload_validator=authentication._profile)
    assert observed.payload_sha256 == value.profile_sha
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(raw, trusted_nlp_keys=value.keys, expected_profile_sha256=value.profile_sha)


def test_multiple_active_profiles_must_all_have_prepared_packages(tmp_path):
    value = Release(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = Example(other_root)
    other.document["revision"] = "2"
    other.save()
    other.run()
    value.inputs["packages"].append(dict(directory=str(other.output), inventory=pin(other.output / "inventory.json"),
        producer_receipt=pin(other.output / "producer-receipt.json")))
    value.catalog["profiles"].append(json.loads((other.output / "release-entry.json").read_bytes()))
    value.save()
    value.run()
    assert len(list((value.output / "profiles").iterdir())) == 2


@pytest.mark.parametrize("fault", ["absent_previous", "extra_field", "missing_package", "extra_package", "duplicate_package", "active_withdrawn"])
def test_closed_manifest_and_exact_active_profile_set(tmp_path, fault):
    value = Release(tmp_path)
    if fault == "absent_previous":
        del value.inputs["previous_catalog"]
    elif fault == "extra_field":
        value.inputs["approve"] = True
    elif fault == "missing_package":
        value.inputs["packages"] = []
    elif fault == "extra_package":
        value.catalog["profiles"] = []
    elif fault == "duplicate_package":
        value.inputs["packages"].append(copy.deepcopy(value.inputs["packages"][0]))
    else:
        value.catalog["withdrawals"] = [value.profile_sha]
    value.save()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


@pytest.mark.parametrize("fault", ["wrong_key", "unknown_kid", "other_curve", "malformed_key", "public_key", "oversized_key", "trust_pin", "duplicate_kid", "noncanonical_trust", "hardlink_key"])
def test_signer_is_bounded_private_and_matches_independent_trust(tmp_path, fault):
    value = Release(tmp_path)
    if fault in {"wrong_key", "other_curve", "public_key"}:
        key = ec.derive_private_key(41, ec.SECP384R1() if fault == "other_curve" else ec.SECP256R1())
        raw = (key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
               if fault == "public_key" else key.private_bytes(serialization.Encoding.PEM,
               serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        value.key_path.write_bytes(raw)
        value.inputs["signer"] = pin(value.key_path)
    elif fault == "unknown_kid":
        value.inputs["kid"] = "other"
    elif fault in {"malformed_key", "oversized_key"}:
        value.key_path.write_bytes(b"not a key" if fault == "malformed_key" else b"k" * (signing.MAX_KEY_BYTES + 1))
        value.inputs["signer"] = pin(value.key_path)
    elif fault == "trust_pin":
        value.inputs["trusted_keys"]["sha256"] = "0" * 64
    elif fault == "duplicate_kid":
        value.trust_path.write_bytes(canonical(value.keys * 2))
        value.inputs["trusted_keys"] = pin(value.trust_path)
    elif fault == "noncanonical_trust":
        value.trust_path.write_bytes(json.dumps(value.keys, indent=2).encode())
        value.inputs["trusted_keys"] = pin(value.trust_path)
    else:
        os.link(value.key_path, tmp_path / "key-link")
    value.write_inputs()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


def test_existing_insecure_key_permissions_are_refused_without_repair(tmp_path):
    value = Release(tmp_path)
    if os.name == "nt":
        import subprocess
        subprocess.run(["icacls", str(value.key_path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
    else:
        os.chmod(value.key_path, 0o644)
    before = value.key_path.stat().st_mode
    with pytest.raises(ValueError, match="owner-only"):
        value.run()
    assert value.key_path.stat().st_mode == before


@pytest.mark.parametrize("fault", ["extra_root", "extra_data", "missing_data", "corrupt_data", "receipt", "inventory", "profile_entry", "inventory_alias"])
def test_complete_prepared_membership_and_integrity_are_reverified(tmp_path, fault):
    value = Release(tmp_path)
    package = value.package.output
    if fault == "extra_root":
        (package / "extra").write_bytes(b"extra")
    elif fault == "extra_data":
        (package / "data" / ("0" * 64)).write_bytes(b"extra")
    elif fault == "missing_data":
        next((package / "data").iterdir()).unlink()
    elif fault == "corrupt_data":
        path = next((package / "data").iterdir())
        path.write_bytes(b"x" * path.stat().st_size)
    elif fault == "receipt":
        doc = json.loads((package / "producer-receipt.json").read_bytes())
        doc["accounting"]["minimum_installed_bytes"] -= 1
        (package / "producer-receipt.json").write_bytes(canonical(doc))
        value.inputs["packages"][0]["producer_receipt"] = pin(package / "producer-receipt.json")
    elif fault == "inventory":
        doc = json.loads((package / "inventory.json").read_bytes())
        doc["files"].append(dict(file="../outside", sha256="a" * 64, size_bytes=1))
        (package / "inventory.json").write_bytes(canonical(doc))
        value.inputs["packages"][0]["inventory"] = pin(package / "inventory.json")
    elif fault == "profile_entry":
        value.catalog["profiles"][0]["compatibility"]["adapters"][0]["adapter_version"] = "2"
    else:
        value.inputs["packages"][0]["inventory"]["path"] = str(package / "data" / ".." / "inventory.json")
    value.save()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


def test_retained_profile_envelopes_are_preserved_exactly_without_resigning(tmp_path, monkeypatch):
    value = Release(tmp_path)
    previous = value.prior()
    original = signing._sign
    calls = []
    def sign(raw, typ, key, kid):
        calls.append(typ)
        return original(raw, typ, key, kid)
    monkeypatch.setattr(signing, "_sign", sign)
    value.run()
    assert calls == [CATALOG_TYPE]
    assert (value.output / "profiles" / value.profile_sha).read_bytes() == (previous / "profiles" / value.profile_sha).read_bytes()


@pytest.mark.parametrize("fault", ["no_reuse", "duplicate_reuse", "other_digest", "other_profile_payload", "wrong_version", "corrupt_envelope", "wrong_domain", "rollback", "issued_rollback", "lost_withdrawal"])
def test_previous_catalog_and_reused_profiles_fail_closed(tmp_path, fault):
    value = Release(tmp_path)
    if fault == "lost_withdrawal":
        value.catalog["withdrawals"] = ["a" * 64]
        value.save()
    previous = value.prior(reuse=fault != "no_reuse")
    if fault == "duplicate_reuse":
        value.inputs["reuse_profiles"] *= 2
    elif fault == "other_digest":
        value.inputs["reuse_profiles"][0]["profile_sha256"] = "a" * 64
    elif fault in {"corrupt_envelope", "wrong_domain"}:
        path = tmp_path / "reused.jws"
        path.write_bytes(b"corrupt" if fault == "corrupt_envelope" else (previous / "catalog.jws").read_bytes())
        value.inputs["reuse_profiles"][0]["envelope"] = pin(path)
    elif fault == "other_profile_payload":
        path = tmp_path / "other-profile.jws"
        document = json.loads((value.package.output / "profile.json").read_bytes())
        document["revision"] = "different"
        path.write_bytes(sign(canonical(document), typ="nlp-profile.v3+jws", private_key=value.key, kid=value.kid))
        value.inputs["reuse_profiles"][0]["envelope"] = pin(path)
    elif fault == "wrong_version":
        path = tmp_path / "wrong-version.jws"
        path.write_bytes(sign((value.package.output / "profile.json").read_bytes(),
            typ="nlp-profile.v2+jws", private_key=value.key, kid=value.kid))
        value.inputs["reuse_profiles"][0]["envelope"] = pin(path)
    elif fault == "rollback":
        value.catalog["sequence"] = 1
    elif fault == "issued_rollback":
        value.catalog["issued_at"] = 0
    elif fault == "lost_withdrawal":
        value.catalog["withdrawals"] = []
    value.save()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


@pytest.mark.parametrize("fault", ["limit", "space", "existing", "overlap"])
def test_bounds_and_fresh_output_are_checked_before_data_streaming(tmp_path, monkeypatch, fault):
    value = Release(tmp_path)
    if fault == "limit":
        value.inputs["maximum_output_bytes"] = 1
        value.write_inputs()
    elif fault == "space":
        monkeypatch.setattr(signing.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    elif fault == "existing":
        value.output.mkdir()
    else:
        value.output = value.package.output
    original = signing.packaging.Source.transfer
    def bounded(self, *args, **kwargs):
        assert self.path.parent.name != "data", "data read before refusal"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(signing.packaging.Source, "transfer", bounded)
    with pytest.raises(ValueError):
        value.run()


def test_source_change_after_signing_prevents_publication(tmp_path, monkeypatch):
    value = Release(tmp_path)
    original = signing._sign
    def changed(raw, typ, key, kid):
        result = original(raw, typ, key, kid)
        if typ == CATALOG_TYPE:
            path = next((value.package.output / "data").iterdir())
            path.write_bytes(b"changed")
        return result
    monkeypatch.setattr(signing, "_sign", changed)
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()
    assert not list(tmp_path.glob(".*.stage-*"))


def test_concurrent_output_is_not_replaced_and_stage_is_cleaned(tmp_path, monkeypatch):
    value = Release(tmp_path)
    original = signing.packaging._exclusive_publish
    def concurrent(stage, output):
        output.mkdir()
        (output / "foreign").write_bytes(b"foreign")
        original(stage, output)
    monkeypatch.setattr(signing.packaging, "_exclusive_publish", concurrent)
    with pytest.raises(OSError):
        value.run()
    assert (value.output / "foreign").read_bytes() == b"foreign"
    assert not list(tmp_path.glob(".*.stage-*"))


@pytest.mark.parametrize("fault", ["receipt", "extra", "replacement_directory"])
def test_staged_mutation_refuses_and_preserves_foreign_replacements(tmp_path, monkeypatch, fault):
    value = Release(tmp_path)
    original = signing.packaging.Source.transfer
    injected = []
    def tamper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        stages = list(tmp_path.glob(".*.stage-*"))
        if not injected and stages and (stages[0] / "producer-receipt.json").exists():
            stage = stages[0]
            if fault == "replacement_directory":
                path = stage / "profiles"
                path.rename(stage / "original-profiles")
                path.mkdir()
            else:
                path = stage / ("producer-receipt.json" if fault == "receipt" else "extra")
                path.write_bytes(b"foreign")
            injected.append(path)
        return result
    monkeypatch.setattr(signing.packaging.Source, "transfer", tamper)
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists() and injected
    if fault != "receipt":
        assert injected[0].exists()
    assert not list(tmp_path.glob(".*.stage-*/producer-receipt.json"))


def test_withdrawal_only_catalog_needs_no_active_profile_packages(tmp_path):
    value = Release(tmp_path)
    value.prior(reuse=False)
    value.catalog["profiles"] = []
    value.catalog["withdrawals"] = [value.profile_sha]
    value.inputs["packages"] = []
    value.save()
    value.run()
    catalog, _ = verify_catalog((value.output / "catalog.jws").read_bytes(), value.keys)
    assert not catalog.profiles and catalog.withdrawals == (value.profile_sha,)
    assert not list((value.output / "profiles").iterdir())
    inventory = json.loads((value.output / "publication-inventory.json").read_bytes())
    assert not inventory["data"] and [row["file"] for row in inventory["files"]] == ["catalog.jws"]


def test_reused_profile_can_retain_a_different_still_trusted_signer(tmp_path):
    value = Release(tmp_path)
    previous = value.prior()
    old_keys = value.keys
    value.key = ec.derive_private_key(41, ec.SECP256R1())
    value.kid = "example-next-key"
    value.keys = old_keys + keys(value.key, value.kid)
    value.trust_path.write_bytes(canonical(value.keys))
    value.key_path.write_bytes(value.key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    value.inputs.update(trusted_keys=pin(value.trust_path), signer=pin(value.key_path), kid=value.kid)
    value.write_inputs()
    value.run()
    catalog_raw = (value.output / "catalog.jws").read_bytes()
    catalog = signed_objects.verify_compact(catalog_raw.decode(), typ=CATALOG_TYPE,
        keys=value.keys, maximum=signing.CATALOG_BYTES, envelope_limit=signing.CATALOG_ENVELOPE_BYTES,
        header_canonicalizer=canonical, payload_validator=ReleaseCatalog.model_validate_json)
    assert catalog.kid == value.kid and catalog.payload.model_dump(mode="json") == value.catalog
    profile_raw = (value.output / "profiles" / value.profile_sha).read_bytes()
    observed = signed_objects.verify_compact(profile_raw.decode(), typ="nlp-profile.v3+jws",
        keys=value.keys, maximum=authentication.MAX_PROFILE_BYTES,
        envelope_limit=authentication.MAX_ENVELOPE_BYTES, header_canonicalizer=canonical,
        payload_validator=authentication._profile)
    assert observed.kid == old_keys[0]["kid"] and observed.payload_sha256 == value.profile_sha
    assert canonical(observed.payload) == (value.package.output / "profile.json").read_bytes()
    assert profile_raw == (previous / "profiles" / value.profile_sha).read_bytes()


def test_output_inside_prepared_package_is_refused_without_mutating_it(tmp_path):
    value = Release(tmp_path)
    value.output = value.package.output / "new-output"
    original = {path.relative_to(value.package.output).as_posix(): pin(path)
        for path in value.package.output.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        value.run()
    assert {path.relative_to(value.package.output).as_posix(): pin(path)
        for path in value.package.output.rglob("*") if path.is_file()} == original
    assert not value.output.exists() and not list(value.package.output.glob(".*.stage-*"))
