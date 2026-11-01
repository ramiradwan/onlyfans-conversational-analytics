"""Sign exact prepared NLP data releases without publishing or approving them.

The closed nlp-release-signing-inputs/v1 manifest pins prepared packages, canonical
catalog/trust documents, a caller-selected private P-256 PEM and optional retained
envelopes. Use --inputs, --inputs-sha256, --output and optionally --validate-only.
Catalog sequence, times and withdrawals are supplied explicitly by the caller.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Annotated, Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from pydantic import Field
from app.nlp.adapter import validate_compatibility
from app.nlp.contracts import Contract, Digest
from app.nlp import profile_authentication as authentication
from app.nlp.release_catalog import (CATALOG_BYTES, CATALOG_TYPE, CATALOG_ENVELOPE_BYTES,
    ReleaseCatalog, ReleaseEntry, canonical, verify_catalog)
from app.persistence import private_files
from app.security import signed_objects
from tools import nlp_profile_packaging as packaging

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_KEY_BYTES = 16 * 1024
PathText = Annotated[str, Field(min_length=1, max_length=32768)]


class Package(Contract):
    directory: PathText
    inventory: packaging.FilePin
    producer_receipt: packaging.FilePin


class ReusedProfile(Contract):
    profile_sha256: Digest
    envelope: packaging.FilePin


class Inputs(Contract):
    schema_: Literal["nlp-release-signing-inputs/v1"] = Field(alias="schema")
    packages: Annotated[tuple[Package, ...], Field(max_length=32)]
    catalog: packaging.FilePin
    previous_catalog: packaging.FilePin | None
    trusted_keys: packaging.FilePin
    signer: packaging.FilePin
    kid: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    reuse_profiles: Annotated[tuple[ReusedProfile, ...], Field(max_length=32)] = ()
    maximum_output_bytes: packaging.PositiveBytes
    minimum_free_bytes: packaging.PositiveBytes


class FileRecord(Contract):
    file: Annotated[str, Field(min_length=1, max_length=80)]
    size_bytes: packaging.PositiveBytes
    sha256: Digest


class Inventory(Contract):
    schema_: Literal["nlp-profile-data-inventory/v1"] = Field(alias="schema")
    signed: Literal[False]
    approved: Literal[False]
    files: Annotated[tuple[FileRecord, ...], Field(min_length=3, max_length=258)]


class Accounting(Contract):
    data_bytes: packaging.PositiveBytes
    artifact_bytes: packaging.PositiveBytes
    installed_receipt_bytes: packaging.PositiveBytes
    minimum_installed_bytes: packaging.PositiveBytes
    minimum_temporary_bytes: packaging.PositiveBytes


class PreparationReceipt(Contract):
    schema_: Literal["nlp-profile-preparation-receipt/v1"] = Field(alias="schema")
    signed: Literal[False]
    approved: Literal[False]
    validation: Literal["unsigned-structural-preparation"]
    runtime_executed: Literal[False]
    inputs_sha256: Digest
    profile_sha256: Digest
    worker_release_sha256: Digest
    inventory_sha256: Digest
    accounting: Accounting


def _document(source, model, maximum, *, normalized=True):
    raw = source.document(maximum)
    value = model.model_validate_json(raw)
    if (canonical(signed_objects.parse(raw, maximum=maximum)) != raw
            or normalized and canonical(value.model_dump(mode="json", by_alias=True)) != raw):
        raise ValueError("canonical metadata required")
    return value, raw


def _private_key(source):
    """Inspect existing input permissions without repairing or exporting a key."""
    source.check_identity()
    if os.name == "nt":
        import msvcrt
        private = private_files._windows_acl_is_owner_only(
            source.path, handle=msvcrt.get_osfhandle(source.stream.fileno()))
    else:
        metadata = os.fstat(source.stream.fileno())
        private = stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_uid == os.getuid()
    if not private:
        raise ValueError("signing key requires existing owner-only permissions")
    source.check_identity()


def _key(source, keys, kid):
    if source.pin.size_bytes > MAX_KEY_BYTES:
        raise ValueError("signing key exceeds byte limit")
    _private_key(source)
    try:
        key = serialization.load_pem_private_key(source.transfer(collect=True), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
        selected = next(item for item in keys if item["kid"] == kid)
        if key.public_key().public_numbers() != signed_objects.public_key(selected["jwk"]).public_numbers():
            raise ValueError()
    except (ValueError, TypeError, StopIteration):
        raise ValueError("signing key differs from selected public trust") from None
    _private_key(source)
    return key


def _sign(raw, typ, key, kid):
    message = (signed_objects.encode(canonical(dict(alg="ES256", kid=kid, typ=typ)))
               + "." + signed_objects.encode(raw)).encode("ascii")
    r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return message + b"." + signed_objects.encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).encode("ascii")


def _profile_envelope(raw, profile, keys):
    observed = signed_objects.verify_compact(raw.decode("ascii"),
        typ=profile.schema_version + "+jws", keys=keys,
        maximum=authentication.MAX_PROFILE_BYTES, envelope_limit=authentication.MAX_ENVELOPE_BYTES,
        header_canonicalizer=canonical, payload_validator=authentication._profile)
    if observed.payload_sha256 != profile.identity_digest():
        raise ValueError("reused profile envelope differs from exact profile")
    return observed


def _membership(directory, expected, identity, data_identity):
    packaging.refuse_alias(directory)
    packaging.refuse_alias(directory / "data")
    if (packaging._identity(directory.stat())[:2] != identity
            or packaging._identity((directory / "data").stat())[:2] != data_identity
            or {path.name for path in directory.iterdir()} != {
                "profile.json", "release-entry.json", "inventory.json", "producer-receipt.json", "data"}
            or {"data/" + path.name for path in (directory / "data").iterdir()} != expected):
        raise ValueError("prepared package membership or identity differs")


def prepare(inputs_path: Path, expected_sha256: str, output: Path, *, validate_only=False):
    packaging.regular(inputs_path)
    with ExitStack() as stack:
        sources = []
        def opened(pin):
            source = packaging.Source(pin, stack)
            sources.append(source)
            return source
        manifest = opened(packaging.FilePin(path=str(inputs_path), size_bytes=inputs_path.stat().st_size,
                                            sha256=expected_sha256))
        inputs, _ = _document(manifest, Inputs, MAX_METADATA_BYTES, normalized=False)
        catalog, catalog_raw = _document(opened(inputs.catalog), ReleaseCatalog, CATALOG_BYTES)
        trust_raw = opened(inputs.trusted_keys).document(MAX_KEY_BYTES)
        keys = authentication._keys(signed_objects.parse(trust_raw, maximum=MAX_KEY_BYTES, canonicalizer=canonical))
        if not any(item["kid"] == inputs.kid for item in keys):
            raise ValueError("selected key is absent from public trust")
        previous = None
        if inputs.previous_catalog is not None:
            source = opened(inputs.previous_catalog)
            if source.pin.size_bytes > CATALOG_ENVELOPE_BYTES:
                raise ValueError("previous catalog exceeds byte limit")
            previous, _ = verify_catalog(source.transfer(collect=True), keys)
            if (catalog.sequence <= previous.sequence or catalog.issued_at < previous.issued_at
                    or not set(previous.withdrawals) <= set(catalog.withdrawals)):
                raise ValueError("catalog state regresses")
        profiles, entries, data, package_checks = {}, {}, {}, []
        for package in inputs.packages:
            directory = Path(package.directory)
            if (not directory.is_absolute() or ".." in directory.parts
                    or Path(package.inventory.path) != directory / "inventory.json"
                    or Path(package.producer_receipt.path) != directory / "producer-receipt.json"
                    or output.is_relative_to(directory) or directory.is_relative_to(output)):
                raise ValueError("prepared metadata must belong to exact package directory")
            packaging.refuse_alias(directory)
            packaging.refuse_alias(directory / "data")
            identity = packaging._identity(directory.stat())[:2]
            data_identity = packaging._identity((directory / "data").stat())[:2]
            inventory, inventory_raw = _document(opened(package.inventory), Inventory, MAX_METADATA_BYTES)
            receipt, _ = _document(opened(package.producer_receipt), PreparationReceipt, MAX_METADATA_BYTES)
            records = {record.file: record for record in inventory.files}
            if len(records) != len(inventory.files) or not {"profile.json", "release-entry.json"} <= records.keys():
                raise ValueError("duplicate or incomplete inventory")
            def file_source(name):
                record = records[name]
                return opened(packaging.FilePin(path=str(directory / name), size_bytes=record.size_bytes, sha256=record.sha256))
            # Do not interpret inventory-controlled arbitrary paths before closing membership.
            if any(name not in {"profile.json", "release-entry.json"}
                   and not (len(name) == 69 and name.startswith("data/")
                            and all(c in "0123456789abcdef" for c in name[5:])) for name in records):
                raise ValueError("inventory contains unsupported members")
            profile_raw = file_source("profile.json").document(authentication.MAX_PROFILE_BYTES)
            profile = authentication._profile(profile_raw)
            entry, entry_raw = _document(file_source("release-entry.json"), ReleaseEntry, MAX_METADATA_BYTES)
            validate_compatibility(profile, entry.compatibility)
            profile_sha = profile.identity_digest()
            if (entry.profile_sha256 != profile_sha or entry.profile_id != profile.profile_id
                    or entry.revision != profile.revision or profile_sha in profiles):
                raise ValueError("duplicate or mismatched profile entry")
            packaging._no_local_paths(profile.model_dump(mode="json"))
            members = sorted((asset for asset in profile.artifacts if asset.role in packaging.DATA_ROLES),
                             key=lambda asset: asset.artifact_id)
            data_bytes = sum(asset.size_bytes for asset in members)
            all_bytes = sum(asset.size_bytes for asset in profile.artifacts)
            receipt_bytes = len(canonical([asset.model_dump(mode="json") for asset in members]))
            accounting = dict(data_bytes=data_bytes, artifact_bytes=all_bytes, installed_receipt_bytes=receipt_bytes,
                minimum_installed_bytes=all_bytes + receipt_bytes, minimum_temporary_bytes=2 * data_bytes + receipt_bytes)
            if (receipt.profile_sha256 != profile_sha or receipt.inventory_sha256 != packaging.digest(inventory_raw)
                    or receipt.accounting.model_dump(mode="json") != accounting
                    or profile.resources.download_bytes < all_bytes
                    or profile.resources.installed_bytes < accounting["minimum_installed_bytes"]
                    or profile.resources.temporary_bytes < accounting["minimum_temporary_bytes"]):
                raise ValueError("prepared receipt or accounting differs")
            expected = {"profile.json": (len(profile_raw), packaging.digest(profile_raw)),
                        "release-entry.json": (len(entry_raw), packaging.digest(entry_raw))}
            for asset in members:
                name = "data/" + asset.sha256
                if name in expected and expected[name] != (asset.size_bytes, asset.sha256):
                    raise ValueError("content-addressed size conflict")
                expected[name] = (asset.size_bytes, asset.sha256)
            if {name: (row.size_bytes, row.sha256) for name, row in records.items()} != expected:
                raise ValueError("inventory differs from complete profile data")
            data_names = {name for name in expected if name.startswith("data/")}
            _membership(directory, data_names, identity, data_identity)
            package_checks.append((directory, data_names, identity, data_identity))
            for name in sorted(data_names):
                source = file_source(name)
                item = data.setdefault(name, dict(file=name, sha256=source.pin.sha256,
                    size_bytes=source.pin.size_bytes, artifacts=[]))
                if item["size_bytes"] != source.pin.size_bytes:
                    raise ValueError("content-addressed size conflict")
                item["artifacts"].extend(dict(profile_sha256=profile_sha, artifact_id=asset.artifact_id)
                    for asset in members if "data/" + asset.sha256 == name)
            profiles[profile_sha] = (profile, profile_raw)
            entries[profile_sha] = entry
        if ({entry.profile_sha256: entry for entry in catalog.profiles} != entries
                or set(profiles) & set(catalog.withdrawals)):
            raise ValueError("catalog must match exact complete active profile set")
        reused = {}
        for item in inputs.reuse_profiles:
            if item.profile_sha256 not in profiles or item.profile_sha256 in reused:
                raise ValueError("duplicate or undeclared reused profile")
            source = opened(item.envelope)
            if source.pin.size_bytes > authentication.MAX_ENVELOPE_BYTES:
                raise ValueError("profile envelope exceeds byte limit")
            raw = source.transfer(collect=True)
            _profile_envelope(raw, profiles[item.profile_sha256][0], keys)
            reused[item.profile_sha256] = raw
        if previous is not None:
            retained = set(profiles) & {entry.profile_sha256 for entry in previous.profiles}
            if not retained <= reused.keys():
                raise ValueError("previously published profiles require pinned reusable envelopes")
        key_source = opened(inputs.signer)
        key = _key(key_source, keys, inputs.kid)
        identities = [(source.identity[0], source.identity[1]) for source in sources]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source identity")
        packaging._output_path(output, sources)

        def outputs(sign):
            def envelope(raw, typ):
                if sign:
                    return _sign(raw, typ, key, inputs.kid)
                message = signed_objects.encode(canonical(dict(alg="ES256", kid=inputs.kid, typ=typ))) + "." + signed_objects.encode(raw)
                return (message + "." + signed_objects.encode(bytes(64))).encode("ascii")
            payload = {"catalog.jws": envelope(catalog_raw, CATALOG_TYPE)}
            for sha, (profile, raw) in sorted(profiles.items()):
                payload["profiles/" + sha] = reused.get(sha) if sha in reused else envelope(raw, profile.schema_version + "+jws")
            files = [dict(file=name, size_bytes=len(raw), sha256=packaging.digest(raw)) for name, raw in sorted(payload.items())]
            inventory = dict(schema="nlp-publication-inventory/v1", approved=False, files=files,
                data=[dict(item, artifacts=sorted(item["artifacts"], key=lambda row: (row["profile_sha256"], row["artifact_id"])))
                      for _, item in sorted(data.items())])
            payload["publication-inventory.json"] = canonical(inventory)
            receipt = dict(schema="nlp-release-signing-receipt/v1", signed=True, approved=False,
                runtime_executed=False, resource_admission="not_evaluated", inputs_sha256=expected_sha256,
                signer_kid=inputs.kid, catalog_payload_sha256=packaging.digest(catalog_raw),
                inventory_sha256=packaging.digest(payload["publication-inventory.json"]),
                profiles=sorted(profiles), reused_profiles=sorted(reused))
            payload["producer-receipt.json"] = canonical(receipt)
            return payload, receipt
        prediction, _ = outputs(False)
        output_bytes = sum(len(raw) for raw in prediction.values())
        if (output_bytes > inputs.maximum_output_bytes
                or len(prediction["publication-inventory.json"]) > MAX_METADATA_BYTES):
            raise ValueError("output exceeds explicit byte limit")
        def space(remaining):
            if shutil.disk_usage(output.parent).free < inputs.minimum_free_bytes + remaining:
                raise ValueError("insufficient free space for output and reserve")
        space(output_bytes)
        for source in sources:
            source.transfer()
        for check in package_checks:
            _membership(*check)
        _private_key(key_source)
        if validate_only:
            return dict(signed=False, approved=False, validation_only=True, profiles=sorted(profiles), output_bytes=output_bytes)
        payload, receipt = outputs(True)
        verify_catalog(payload["catalog.jws"], keys)
        for sha, (profile, _) in profiles.items():
            _profile_envelope(payload["profiles/" + sha], profile, keys)
        if sum(len(raw) for raw in payload.values()) != output_bytes:
            raise ValueError("signed output length differs")
        stage = Path(tempfile.mkdtemp(prefix="." + output.name + ".stage-", dir=output.parent))
        directories = {stage: packaging._identity(stage.stat())[:2]}
        owned = {}
        try:
            private_files.apply_private_directory_security(stage)
            (stage / "profiles").mkdir(mode=0o700)
            private_files.apply_private_directory_security(stage / "profiles")
            directories[stage / "profiles"] = packaging._identity((stage / "profiles").stat())[:2]
            remaining = output_bytes
            for name, raw in payload.items():
                path = stage / name
                with path.open("xb") as target:
                    owned[path] = packaging._identity(os.fstat(target.fileno()))[:2]
                    private_files.apply_private_file_security(path)
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                remaining -= len(raw)
                space(remaining)
            for source in sources:
                source.transfer()
            _private_key(key_source)
            for check in package_checks:
                _membership(*check)
            for directory, identity in directories.items():
                packaging.refuse_alias(directory)
                if packaging._identity(directory.stat())[:2] != identity:
                    raise ValueError("staged directory identity differs")
            if ({path.name for path in stage.iterdir()} != {"catalog.jws", "profiles", "publication-inventory.json", "producer-receipt.json"}
                    or {"profiles/" + path.name for path in (stage / "profiles").iterdir()} != {name for name in payload if name.startswith("profiles/")}):
                raise ValueError("staged membership differs")
            for name, raw in payload.items():
                path = packaging.regular(stage / name)
                if packaging._identity(path.stat())[:2] != owned[path]:
                    raise ValueError("staged file identity differs")
                with ExitStack() as verification:
                    packaging.Source(packaging.FilePin(path=str(path), size_bytes=len(raw),
                        sha256=packaging.digest(raw)), verification).transfer()
            packaging._output_path(output, sources)
            space(0)
            private_files.sync_directory(stage / "profiles")
            private_files.sync_directory(stage)
            packaging._exclusive_publish(stage, output)
            private_files.sync_directory(output.parent)
            return receipt
        finally:
            if stage.exists() and packaging._identity(stage.stat())[:2] == directories[stage]:
                for path, identity in reversed(owned.items()):
                    try:
                        packaging.refuse_alias(path)
                        if packaging._identity(path.stat())[:2] == identity:
                            path.unlink()
                    except (OSError, ValueError):
                        pass
                for directory, identity in reversed(directories.items()):
                    try:
                        packaging.refuse_alias(directory)
                        if packaging._identity(directory.stat())[:2] == identity:
                            directory.rmdir()
                    except (OSError, ValueError):
                        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.inputs, args.inputs_sha256, args.output, validate_only=args.validate_only)
    except (ValueError, OSError, private_files.PrivateFileSecurityError):
        parser.exit(1, "NLP release signing refused.\n")
    print(canonical(result).decode("ascii"))


if __name__ == "__main__":
    main()
