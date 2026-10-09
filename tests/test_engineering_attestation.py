"""Contract tests for the protected engineering-attestation producer."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from tools import engineering_attestation as producer

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
VECTOR_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "engineering-attestation-v1-ed25519.json"
)
MANIFEST_KEY = json.loads((ROOT / "extension" / "manifest.json").read_text())["key"]
REVIEW_CONFIGURATION = producer.ReviewConfiguration(
    repository="review-owner/evidence-control",
    default_branch="trunk",
    projection_path="projection/review-state.json",
    projection_digest_path="projection/review-state.sha256",
)


def _openssl_executable() -> str:
    candidates = [
        shutil.which("openssl"),
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("OpenSSL is not installed on this test host")


def _zip_bytes(
    entries: dict[str, bytes],
    *,
    timestamp=(1980, 1, 1, 0, 0, 0),
    external_attr: int = 0,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = external_attr or 1
            archive.writestr(info, data)
    payload = bytearray(output.getvalue())
    cursor = 0
    while True:
        cursor = payload.find(b"PK\x01\x02", cursor)
        if cursor < 0:
            break
        struct.pack_into("<I", payload, cursor + 38, external_attr)
        name_length, extra_length, comment_length = struct.unpack_from(
            "<HHH", payload, cursor + 28
        )
        cursor += 46 + name_length + extra_length + comment_length
    return bytes(payload)


def _chrome_zip(
    *,
    timestamp=(1980, 1, 1, 0, 0, 0),
    external_attr: int = 0,
    manifest_update: dict | None = None,
    target: str = "chrome132",
) -> tuple[str, bytes]:
    manifest = {
        "key": MANIFEST_KEY,
        "manifest_version": 3,
        "version": "2.0.0",
        "minimum_chrome_version": "132",
        "optional_host_permissions": ["https://onlyfans.com/*"],
        "externally_connectable": {"matches": ["http://bridge.localhost:17871/*"]},
        "web_accessible_resources": [
            {"resources": ["setup.html"], "matches": ["http://bridge.localhost/*"]}
        ],
        "content_security_policy": {
            "extension_pages": "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871; frame-ancestors 'none';"
        },
    }
    manifest.update(manifest_update or {})
    outputs = {
        "background.js": b"export const ready = true;\n",
        "manifest.json": (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    }
    metadata = {
        "schema": producer.EXTENSION_BUILD_SCHEMA,
        "extension_version": "2.0.0",
        "extension_id": producer.EXPECTED_EXTENSION_ID,
        "determinism_verified": True,
        "outputs": {
            name: f"sha256:{hashlib.sha256(data).hexdigest()}"
            for name, data in outputs.items()
        },
        "target": target,
    }
    entries = outputs | {
        "build-meta.json": (json.dumps(metadata, sort_keys=True) + "\n").encode()
    }
    return (
        "OnlyFans-Conversational-Analytics-Agent-2.0.0-chrome.zip",
        _zip_bytes(entries, timestamp=timestamp, external_attr=external_attr),
    )


def _actions_artifact(*, chrome_zip: bytes | None = None) -> tuple[bytes, str]:
    name, default_chrome_zip = _chrome_zip()
    chrome_zip = default_chrome_zip if chrome_zip is None else chrome_zip
    installer_name = "OnlyFans-Conversational-Analytics-Setup-0.7.5-x64.exe"
    installer = b"signed-installer"
    sums = (
        f"{hashlib.sha256(chrome_zip).hexdigest()} *{name}\n"
        f"{hashlib.sha256(installer).hexdigest()} *{installer_name}\n"
    ).encode("ascii")
    archive = _zip_bytes(
        {name: chrome_zip, installer_name: installer, "sha256sums.txt": sums}
    )
    return archive, f"sha256:{hashlib.sha256(archive).hexdigest()}"


def _mark_first_zip_entry_encrypted(data: bytes) -> bytes:
    payload = bytearray(data)
    local = payload.find(b"PK\x03\x04")
    central = payload.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    local_flags = struct.unpack_from("<H", payload, local + 6)[0]
    central_flags = struct.unpack_from("<H", payload, central + 8)[0]
    struct.pack_into("<H", payload, local + 6, local_flags | 0x1)
    struct.pack_into("<H", payload, central + 8, central_flags | 0x1)
    return bytes(payload)


def test_strict_json_rejects_duplicate_keys_at_any_depth() -> None:
    assert producer.load_json_strict(b'{"a":1}', label="fixture") == {"a": 1}
    for malformed in (b'{"a":1,"a":2}', b'{"outer":{"x":1,"x":2}}'):
        with pytest.raises(producer.ContractError, match="duplicate JSON object key"):
            producer.load_json_strict(malformed, label="fixture")


# ZIP creator-platform metadata changes these byte-valued parameter IDs.
# Preserve the Windows archive cases as explicit required CI obligations.
@pytest.mark.windows_compat
@pytest.mark.parametrize(
    "archive",
    [
        _zip_bytes({"../escape.js": b"unsafe"}),
        _zip_bytes({"/absolute.js": b"unsafe"}),
        _zip_bytes({"directory/entry.js": b"unsafe"}).replace(
            b"directory/entry.js", b"directory\\entry.js"
        ),
        _zip_bytes({"Entry.js": b"one", "entry.js": b"two"}),
        _zip_bytes({"link": b"target"}, external_attr=(stat.S_IFLNK | 0o777) << 16),
        _mark_first_zip_entry_encrypted(_zip_bytes({"encrypted.js": b"ciphertext"})),
    ],
)
def test_zip_reader_rejects_unsafe_duplicate_encrypted_and_symlink_entries(
    archive: bytes,
) -> None:
    with pytest.raises(producer.ContractError):
        producer._read_exact_zip_entries(archive, label="fixture ZIP")
    for malformed in (b'{"bad":NaN}', b'{"bad":Infinity}', b'{"bad":-Infinity}'):
        with pytest.raises(producer.ContractError, match="non-standard JSON"):
            producer.load_json_strict(malformed, label="fixture")


def test_github_api_uses_the_neutral_protocol_user_agent(monkeypatch) -> None:
    observed: list[Any] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def open_request(request, *, timeout: int):
        observed.append(request)
        assert timeout == 60
        return Response()

    monkeypatch.setattr(producer.urllib.request, "urlopen", open_request)
    assert producer.GitHubApi("token").get("/app") == {}
    assert observed[0].get_header("User-agent") == "engineering-attestation/1.0"
    assert observed[0].get_header("Authorization") == "Bearer token"

    redirected = producer.urllib.request.HTTPRedirectHandler().redirect_request(
        observed[0],
        None,
        302,
        "Found",
        {},
        "https://artifact-storage.example/download",
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_derived_workflow_secrets_are_masked_without_accepting_newlines(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    producer._mask_workflow_value("derived-token")
    assert capsys.readouterr().out == "::add-mask::derived-token\n"
    producer._mask_workflow_value("derived%token")
    assert capsys.readouterr().out == "::add-mask::derived%25token\n"
    with pytest.raises(producer.ContractError, match="contains a newline"):
        producer._mask_workflow_value("not\nsafe")


def test_projection_digest_ignores_only_the_contract_ephemeral_fields() -> None:
    left = {"policy": {"status": "approved"}, "run_id": "one"}
    right = {"runner_name": "two", "policy": {"status": "approved"}}
    changed = {"policy": {"status": "changed"}, "run_id": "one"}
    assert producer.canonical_projection_sha256(left) == (
        producer.canonical_projection_sha256(right)
    )
    assert producer.canonical_projection_sha256(left) != (
        producer.canonical_projection_sha256(changed)
    )


def test_attestation_final_bytes_are_stable_and_signature_is_the_only_omission() -> None:
    attestation = {
        "schema_version": "1.0",
        "attestation_id": "chrome-extension-1-1",
        "legal_projection": {"b": 2, "a": 1},
        "provenance": {
            "algorithm": "ed25519",
            "signed_at": "2026-08-28T12:00:00Z",
            "signer_id": producer.SIGNER_ID,
            "signature": "c2lnbmF0dXJl",
        },
    }
    payload = producer.attestation_signing_payload(attestation)
    parsed_payload = json.loads(payload)
    assert parsed_payload["provenance"] == {
        "algorithm": "ed25519",
        "signed_at": "2026-08-28T12:00:00Z",
        "signer_id": producer.SIGNER_ID,
    }
    assert producer.serialize_final_attestation(attestation).endswith(b"\n")
    assert producer.serialize_final_attestation(attestation) == (
        producer.serialize_final_attestation(copy.deepcopy(attestation))
    )


def test_evidence_pr_body_keeps_audit_context_outside_the_trust_root(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "321")
    source = producer.QualifiedSource(
        run_id=123,
        run_attempt=2,
        product_ci_run_id=124,
        source_commit="a" * 40,
        artifact_id=456,
        artifact_name="windows-package-v0.7.5",
        artifact_server_digest="sha256:" + "b" * 64,
        archive_download_url="https://api.github.com/artifact/456",
    )
    chrome_zip = producer.QualifiedChromeZip(
        filename="OnlyFans-Conversational-Analytics-Agent-2.0.0-chrome.zip",
        version="2.0.0",
        sha256="c" * 64,
        size_bytes=100,
        bytes=b"zip",
        actions_archive_sha256="d" * 64,
    )
    projection = producer.ReviewProjection(
        source_commit="e" * 40,
        current_commit="f" * 40,
        digest="1" * 64,
        value={"state": "reviewed"},
    )
    body = producer._pr_body(
        source=source,
        release_tag="v0.7.5",
        chrome_zip=chrome_zip,
        projection=projection,
        attestation_sha256="2" * 64,
        signer_fingerprint="sha256:" + "3" * 64,
        identity=producer.HandoffIdentity(4, 5, 6, 7, "review-evidence"),
    )
    assert "The body is audit context, not cryptographic evidence." in body
    assert (
        "A `MATCH` result and an accepted evidence record establish producer "
        "identity and exact artifact provenance. They do not authorize "
        "publication or release."
    ) in body
    assert REVIEW_CONFIGURATION.repository not in body


def test_review_conformance_vector_matches_product_canonicalization_and_openssl(
    tmp_path: Path,
) -> None:
    vector = producer.load_json_strict(VECTOR_PATH.read_bytes(), label="vector")
    assert vector["schema_version"] == "1.0"
    assert "TEST-ONLY" in vector["purpose"]
    projection = vector["projection"]
    assert producer.canonical_projection_bytes(projection["object"]).decode(
        "utf-8"
    ) == projection["canonical_json_utf8"]
    assert producer.canonical_projection_sha256(projection["object"]) == projection[
        "sha256"
    ]

    ed25519_vector = vector["ed25519"]
    unsigned_attestation = ed25519_vector["unsigned_attestation"]
    attestation = copy.deepcopy(unsigned_attestation)
    payload = producer.attestation_signing_payload(attestation)
    assert payload.decode("utf-8") == ed25519_vector["signing_payload_utf8"]
    assert hashlib.sha256(payload).hexdigest() == ed25519_vector[
        "signing_payload_sha256"
    ]

    private = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(ed25519_vector["test_private_seed_hex"])
    )
    private_path = tmp_path / "non-production-vector-private.pem"
    public_path = tmp_path / "non-production-vector-public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_text(
        ed25519_vector["public_key_pem"], encoding="ascii", newline="\n"
    )
    openssl = _openssl_executable()
    producer.verify_conformance_vector(VECTOR_PATH, tmp_path, openssl=openssl)
    signature = producer.sign_ed25519(payload, private_path, openssl=openssl)
    signature_base64 = base64.b64encode(signature).decode("ascii")
    assert signature_base64 == ed25519_vector["signature_base64"]
    producer.verify_ed25519(payload, signature, public_path, openssl=openssl)
    assert producer.spki_fingerprint(
        ed25519_vector["public_key_pem"].encode("ascii"), openssl=openssl
    ) == ed25519_vector["public_key_spki_fingerprint"]

    attestation["provenance"]["signature"] = signature_base64
    final_attestation = producer.serialize_final_attestation(attestation)
    assert hashlib.sha256(final_attestation).hexdigest() == ed25519_vector[
        "final_attestation_sha256"
    ]

    negative = vector["negative_vectors"]
    with pytest.raises(producer.ContractError, match="duplicate JSON object key"):
        producer.load_json_strict(negative["duplicate_key_json"].encode(), label="vector")
    with pytest.raises(producer.ContractError, match="non-standard JSON"):
        producer.load_json_strict(negative["nonfinite_json"].encode(), label="vector")
    with pytest.raises(ValueError):
        base64.b64decode(negative["malformed_base64_signature"], validate=True)
    assert negative["wrong_signer_id"] != producer.SIGNER_ID

    changed_vector = tmp_path / "changed-vector.json"
    changed_vector.write_bytes(VECTOR_PATH.read_bytes() + b" ")
    with pytest.raises(producer.ContractError, match="consumer contract"):
        producer.verify_conformance_vector(changed_vector, tmp_path, openssl=openssl)


def test_openssl_ed25519_signing_and_spki_fingerprint_match_cryptography(
    tmp_path: Path,
) -> None:
    private = ed25519.Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path.write_bytes(public_pem)
    payload = b"canonical attestation payload"

    openssl = _openssl_executable()
    derived = producer.derive_public_key(private_path, openssl=openssl)
    signature = producer.sign_ed25519(payload, private_path, openssl=openssl)
    producer.verify_ed25519(payload, signature, public_path, openssl=openssl)

    expected_der = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert derived.replace(b"\r\n", b"\n") == public_pem.replace(b"\r\n", b"\n")
    assert producer.spki_fingerprint(derived, openssl=openssl) == (
        "sha256:" + hashlib.sha256(expected_der).hexdigest()
    )
    private.public_key().verify(signature, payload)


def test_protected_signer_rejects_key_identity_and_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    openssl = _openssl_executable()
    private = ed25519.Ed25519PrivateKey.generate()
    other_private = ed25519.Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    other_public_path = tmp_path / "other-public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path.write_bytes(public_pem)
    other_public_path.write_bytes(
        other_private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    fingerprint = producer.spki_fingerprint(public_pem, openssl=openssl)

    assert producer.validate_signer_material(
        private_path,
        public_path,
        expected_signer_id=producer.SIGNER_ID,
        expected_fingerprint=fingerprint,
        openssl=openssl,
    ) == fingerprint
    with pytest.raises(producer.ContractError, match="does not match committed"):
        producer.validate_signer_material(
            private_path,
            other_public_path,
            expected_signer_id=producer.SIGNER_ID,
            expected_fingerprint=fingerprint,
            openssl=openssl,
        )
    with pytest.raises(producer.ContractError, match="signer ID"):
        producer.validate_signer_material(
            private_path,
            public_path,
            expected_signer_id="wrong-signer",
            expected_fingerprint=fingerprint,
            openssl=openssl,
        )
    with pytest.raises(producer.ContractError, match="fingerprint mismatch"):
        producer.validate_signer_material(
            private_path,
            public_path,
            expected_signer_id=producer.SIGNER_ID,
            expected_fingerprint="sha256:" + "0" * 64,
            openssl=openssl,
        )


def test_secret_key_files_are_created_once_with_owner_only_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "private.pem"
    monkeypatch.setenv("TEST_PRIVATE_KEY_B64", base64.b64encode(b"secret").decode())

    producer._decode_secret_to_file("TEST_PRIVATE_KEY_B64", destination)

    assert destination.read_bytes() == b"secret"
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(producer.ContractError, match="unable to restrict"):
        producer._decode_secret_to_file("TEST_PRIVATE_KEY_B64", destination)


@pytest.mark.parametrize(
    "manifest_update,target",
    [
        ({"minimum_chrome_version": "116"}, "chrome132"),
        ({}, "chrome116"),
        ({"optional_host_permissions": ["https://onlyfans.com/*", "http://127.0.0.1:17871/*"]}, "chrome132"),
        ({"host_permissions": []}, "chrome132"),
        ({"content_security_policy": {"extension_pages": "script-src 'self'; connect-src *;"}}, "chrome132"),
        ({"externally_connectable": {"matches": ["http://127.0.0.1:17871/*"]}}, "chrome132"),
        ({"web_accessible_resources": []}, "chrome132"),
        ({"web_accessible_resources": [{"resources": ["*"], "matches": ["http://bridge.localhost/*"]}]}, "chrome132"),
        ({"web_accessible_resources": [{"resources": ["setup.html"], "matches": ["<all_urls>"]}]}, "chrome132"),
        ({"content_security_policy": {"extension_pages": "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871;"}}, "chrome132"),
    ],
)
def test_attestation_refuses_an_unqualified_companion_transport_policy(manifest_update, target):
    _, archive = _chrome_zip(manifest_update=manifest_update, target=target)
    outer, digest = _actions_artifact(chrome_zip=archive)
    with pytest.raises(producer.ContractError, match="qualified"):
        producer.qualify_downloaded_artifact(outer, expected_server_digest=digest, release_tag="v2.0.0")


def test_qualified_actions_artifact_binds_exact_inner_chrome_zip() -> None:
    archive, server_digest = _actions_artifact()
    qualified = producer.qualify_downloaded_artifact(
        archive, expected_server_digest=server_digest, release_tag="v2.0.0"
    )
    name, chrome_zip = _chrome_zip()
    assert qualified.filename == name
    assert qualified.version == "2.0.0"
    assert qualified.bytes == chrome_zip
    assert qualified.sha256 == hashlib.sha256(chrome_zip).hexdigest()
    assert qualified.size_bytes == len(chrome_zip)


def test_superseded_extension_build_schema_is_refused() -> None:
    assert producer.EXTENSION_BUILD_SCHEMA == "ofca-extension-build/v4"

    _, chrome_zip = _chrome_zip()
    with zipfile.ZipFile(io.BytesIO(chrome_zip)) as source:
        entries = {entry: source.read(entry) for entry in source.namelist()}
    metadata = json.loads(entries["build-meta.json"])
    metadata["schema"] = "ofca-extension-build/v3"
    entries["build-meta.json"] = (
        json.dumps(metadata, sort_keys=True) + "\n"
    ).encode()
    superseded_zip = _zip_bytes(entries)
    superseded_archive, superseded_server_digest = _actions_artifact(
        chrome_zip=superseded_zip
    )

    with pytest.raises(
        producer.ContractError, match="schema is not ofca-extension-build/v4"
    ):
        producer.qualify_downloaded_artifact(
            superseded_archive,
            expected_server_digest=superseded_server_digest,
            release_tag="v2.0.0",
        )


def test_manifest_public_key_derives_the_pinned_extension_identity() -> None:
    assert producer.extension_id_from_manifest_key(MANIFEST_KEY) == (
        producer.EXPECTED_EXTENSION_ID
    )
    wrong_key = base64.b64encode(b"different extension key").decode("ascii")
    assert producer.extension_id_from_manifest_key(wrong_key) != (
        producer.EXPECTED_EXTENSION_ID
    )
    with pytest.raises(producer.ContractError, match="strict Base64"):
        producer.extension_id_from_manifest_key("***not-base64***")


def test_actions_artifact_rejects_server_digest_zip_metadata_and_inner_tampering() -> None:
    archive, server_digest = _actions_artifact()
    with pytest.raises(producer.ContractError, match="server digest"):
        producer.qualify_downloaded_artifact(
            archive,
            expected_server_digest="sha256:" + "0" * 64,
            release_tag="v2.0.0",
        )

    _, bad_time_zip = _chrome_zip(timestamp=(2026, 8, 28, 12, 0, 0))
    bad_archive, bad_server_digest = _actions_artifact(chrome_zip=bad_time_zip)
    with pytest.raises(producer.ContractError, match="non-deterministic timestamp"):
        producer.qualify_downloaded_artifact(
            bad_archive,
            expected_server_digest=bad_server_digest,
            release_tag="v2.0.0",
        )

    _, bad_metadata_zip = _chrome_zip(external_attr=0x20)
    bad_metadata_archive, bad_metadata_server_digest = _actions_artifact(
        chrome_zip=bad_metadata_zip
    )
    with pytest.raises(producer.ContractError, match="non-deterministic metadata"):
        producer.qualify_downloaded_artifact(
            bad_metadata_archive,
            expected_server_digest=bad_metadata_server_digest,
            release_tag="v2.0.0",
        )

    _, chrome_zip = _chrome_zip()
    with zipfile.ZipFile(io.BytesIO(chrome_zip)) as source:
        entries = {entry: source.read(entry) for entry in source.namelist()}

    wrong_manifest = json.loads(entries["manifest.json"])
    wrong_manifest["key"] = base64.b64encode(b"different extension key").decode(
        "ascii"
    )
    entries["manifest.json"] = (
        json.dumps(wrong_manifest, sort_keys=True) + "\n"
    ).encode()
    wrong_metadata = json.loads(entries["build-meta.json"])
    wrong_metadata["outputs"]["manifest.json"] = (
        "sha256:" + hashlib.sha256(entries["manifest.json"]).hexdigest()
    )
    entries["build-meta.json"] = (
        json.dumps(wrong_metadata, sort_keys=True) + "\n"
    ).encode()
    wrong_identity_zip = _zip_bytes(entries)
    wrong_identity_archive, wrong_identity_server_digest = _actions_artifact(
        chrome_zip=wrong_identity_zip
    )
    with pytest.raises(producer.ContractError, match="wrong extension identity"):
        producer.qualify_downloaded_artifact(
            wrong_identity_archive,
            expected_server_digest=wrong_identity_server_digest,
            release_tag="v2.0.0",
        )

    _, chrome_zip = _chrome_zip()
    with zipfile.ZipFile(io.BytesIO(chrome_zip)) as source:
        entries = {entry: source.read(entry) for entry in source.namelist()}
    entries["background.js"] = b"tampered"
    tampered_zip = _zip_bytes(entries)
    tampered_archive, tampered_server_digest = _actions_artifact(
        chrome_zip=tampered_zip
    )
    with pytest.raises(producer.ContractError, match="output digest mismatch"):
        producer.qualify_downloaded_artifact(
            tampered_archive,
            expected_server_digest=tampered_server_digest,
            release_tag="v2.0.0",
        )

    _, chrome_zip = _chrome_zip()
    with zipfile.ZipFile(io.BytesIO(chrome_zip)) as source:
        entries = {entry: source.read(entry) for entry in source.namelist()}

    metadata = json.loads(entries["build-meta.json"])
    metadata["outputs"]["background.js"] = hashlib.sha256(
        entries["background.js"]
    ).hexdigest()
    entries["build-meta.json"] = (
        json.dumps(metadata, sort_keys=True) + "\n"
    ).encode()

    bare_digest_zip = _zip_bytes(entries)
    bare_digest_archive, bare_digest_server_digest = _actions_artifact(
        chrome_zip=bare_digest_zip
    )

    with pytest.raises(
        producer.ContractError,
        match="output digest has invalid format",
    ):
        producer.qualify_downloaded_artifact(
            bare_digest_archive,
            expected_server_digest=bare_digest_server_digest,
            release_tag="v2.0.0",
        )

    with pytest.raises(producer.ContractError, match="Agent version differ"):
        producer.qualify_downloaded_artifact(
            archive,
            expected_server_digest=server_digest,
            release_tag="v2.0.1",
        )


class _SourceApi:
    def __init__(self) -> None:
        self.run: dict[str, Any] = {
            "workflow_id": 77,
            "path": producer.WINDOWS_PACKAGE_WORKFLOW,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "v0.7.5",
            "head_repository": {"full_name": producer.PRODUCT_REPOSITORY},
            "head_sha": "b" * 40,
            "run_attempt": 2,
        }
        self.artifact = {
            "id": 91,
            "name": "windows-package-v0.7.5",
            "expired": False,
            "digest": "sha256:" + "c" * 64,
            "archive_download_url": "https://api.github.com/artifact/91",
        }
        self.unsigned_artifact = {
            "id": 90,
            "name": "windows-package-unsigned-v0.7.5",
            "expired": False,
            "digest": "sha256:" + "d" * 64,
            "archive_download_url": "https://api.github.com/artifact/90",
        }
        self.jobs: list[dict[str, Any]] = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 42,
                "head_sha": "b" * 40,
            }
            for name in sorted(producer.REQUIRED_WINDOWS_JOB_NAMES)
        ]
        self.workflow = {
            "id": 77,
            "path": producer.WINDOWS_PACKAGE_WORKFLOW,
        }
        self.product_ci_workflow = {
            "id": 88,
            "path": producer.PRODUCT_CI_WORKFLOW,
            "state": "active",
        }
        self.product_ci_run: dict[str, Any] = {
            "id": 43,
            "workflow_id": 88,
            "path": producer.PRODUCT_CI_WORKFLOW,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_branch": producer.PRODUCT_DEFAULT_BRANCH,
            "head_sha": "b" * 40,
            "run_attempt": 1,
            "repository": {"full_name": producer.PRODUCT_REPOSITORY},
            "head_repository": {"full_name": producer.PRODUCT_REPOSITORY},
        }
        self.product_ci_jobs: list[dict[str, Any]] = [
            {
                "id": 4300 + index,
                "name": name,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "run_id": 43,
                "head_sha": "b" * 40,
            }
            for index, name in enumerate(sorted(producer.REQUIRED_PRODUCT_CI_JOB_NAMES))
        ]
        self.product_ci_source = (
            "name: CI\njobs:\n" + "".join(
                f"  {name}:\n    runs-on: ubuntu-latest\n"
                for name in sorted(producer.REQUIRED_PRODUCT_CI_JOB_NAMES)
            )
        ).encode()
        self.product_ci_query_count = 0
        self.tag_commit = "b" * 40
        self.comparison_status = "ahead"

    def set_source_commit(self, sha: str) -> None:
        self.run["head_sha"] = sha
        self.tag_commit = sha
        for job in self.jobs:
            job["head_sha"] = sha
        self.product_ci_run["head_sha"] = sha
        for job in self.product_ci_jobs:
            job["head_sha"] = sha

    def get(self, path: str) -> Any:
        if path.endswith("/actions/runs/42"):
            return self.run
        if path.endswith("/actions/workflows/77"):
            return self.workflow
        if path.endswith("/actions/workflows/ci.yml"):
            return self.product_ci_workflow
        if "/actions/workflows/88/runs?" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert query == {
                "branch": [producer.PRODUCT_DEFAULT_BRANCH],
                "event": ["push"],
                "head_sha": [self.run["head_sha"]],
                "status": ["success"],
                "per_page": ["100"],
            }
            self.product_ci_query_count += 1
            return {"total_count": 1, "workflow_runs": [self.product_ci_run]}
        if path.endswith("/actions/runs/43/jobs?filter=all&per_page=100&page=1"):
            return {
                "total_count": len(self.product_ci_jobs),
                "jobs": self.product_ci_jobs,
            }
        if path.endswith(f"/git/commits/{self.product_ci_run['head_sha']}"):
            return {"tree": {"sha": "1" * 40}}
        for tree_sha, name, kind, mode, sha in (
            ("1", ".github", "tree", "040000", "2"),
            ("2", "workflows", "tree", "040000", "3"),
            ("3", "ci.yml", "blob", "100644", "4"),
        ):
            if path.endswith(f"/git/trees/{tree_sha * 40}"):
                return {"tree": [{"path": name, "type": kind, "mode": mode, "sha": sha * 40}]}
        if path.endswith(f"/git/blobs/{'4' * 40}"):
            return {"encoding": "base64", "size": len(self.product_ci_source),
                    "content": base64.b64encode(self.product_ci_source).decode()}
        if path.endswith("/actions/runs/42/attempts/2/jobs?per_page=100"):
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        if path.endswith("/actions/runs/42/artifacts?per_page=100"):
            artifacts = [self.unsigned_artifact, self.artifact]
            return {"total_count": len(artifacts), "artifacts": artifacts}
        if "/git/ref/tags/" in path:
            return {"object": {"type": "commit", "sha": self.tag_commit}}
        if "/compare/" in path:
            return {"status": self.comparison_status}
        raise AssertionError(path)


def test_source_qualification_re_resolves_tag_baseline_and_exact_artifact() -> None:
    result = producer.qualify_windows_package_source(
        _SourceApi(),
        run_id=42,
        release_tag="v0.7.5",
        baseline_sha="a" * 40,
        workflow_sha="b" * 40,
    )
    assert result.source_commit == "b" * 40
    assert result.product_ci_run_id == 43
    assert result.artifact_id == 91
    assert result.artifact_server_digest == "sha256:" + "c" * 64


def test_source_qualification_requires_the_workflow_jobs_and_exact_artifact_set() -> None:
    failed_job = _SourceApi()
    failed_job.jobs[0]["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="required Windows package job"):
        producer.qualify_windows_package_source(
            failed_job,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )

    extra_artifact = _SourceApi()
    extra_artifact.unsigned_artifact["name"] = "unexpected"
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        producer.qualify_windows_package_source(
            extra_artifact,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )


def test_source_qualification_requires_successful_main_push_product_ci() -> None:
    pull_request_run = _SourceApi()
    pull_request_run.product_ci_run["event"] = "pull_request"
    with pytest.raises(producer.ContractError, match="Product CI run event"):
        producer.qualify_windows_package_source(
            pull_request_run,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )

    wrong_branch = _SourceApi()
    wrong_branch.product_ci_run["head_branch"] = "develop"
    with pytest.raises(producer.ContractError, match="Product CI run head_branch"):
        producer.qualify_windows_package_source(
            wrong_branch,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )

    failed_job = _SourceApi()
    failed_job.product_ci_jobs[0]["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="required Product CI job"):
        producer.qualify_windows_package_source(
            failed_job,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )

    wrong_job_set = _SourceApi()
    wrong_job_set.product_ci_jobs[0]["name"] = "replacement-job"
    with pytest.raises(producer.ContractError, match="exact required job set"):
        producer.qualify_windows_package_source(
            wrong_job_set,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )


def test_resolver_and_signer_qualification_each_recheck_product_ci() -> None:
    api = _SourceApi()
    for _ in range(2):
        producer.qualify_windows_package_source(
            api,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )
    assert api.product_ci_query_count == 2


def _sharded_source_api(version: str = "sharded-v1") -> _SourceApi:
    api = _SourceApi()
    if version == "sharded-v1":
        # Freeze the historical policy rather than reconstructing it from the
        # current workflow, which now requires two Windows regression jobs.
        api.product_ci_source = (
            "name: CI\nenv:\n  CI_POLICY_VERSION: sharded-v1\njobs:\n"
            + "".join(f"  {name}:\n    runs-on: ubuntu-latest\n"
                      for name in sorted(producer.SHARDED_PRODUCT_CI_JOB_IDS))
        ).encode()
        names = producer.REQUIRED_SHARDED_PRODUCT_CI_JOB_NAMES
    else:
        assert version in {"sharded-v2", "sharded-v2-pr-cutover"}
        # Literal source fixtures retain both historical policies independently
        # of today's browser topology and manual qualification inputs.
        api.product_ci_source = (ROOT / f"tests/fixtures/product-ci-{version}.yml").read_bytes()
        names = producer.REQUIRED_SHARDED_V2_PRODUCT_CI_JOB_NAMES
    api.product_ci_jobs = [
        {"id": 4300 + index, "name": name, "status": "completed", "conclusion": "success",
         "run_id": 43, "run_attempt": 1, "head_sha": "b" * 40}
        for index, name in enumerate(sorted(names))
    ]
    return api


@pytest.mark.parametrize("missing_lane", ["analytics-integration-3", "analytics-scale-qualification"])
@pytest.mark.parametrize("version", ["sharded-v1", "sharded-v2", "sharded-v2-pr-cutover"])
def test_source_versioned_sharded_ci_qualifies_every_actual_lane(missing_lane, version) -> None:
    api = _sharded_source_api(version)
    assert producer.qualify_product_ci_source(api, source_commit="b" * 40) == 43
    api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != missing_lane]
    with pytest.raises(producer.ContractError, match="exact required job set"):
        producer.qualify_product_ci_source(api, source_commit="b" * 40)


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
@pytest.mark.parametrize("version", ["sharded-v1", "sharded-v2", "sharded-v2-pr-cutover"])
def test_source_policy_accepts_normal_git_line_endings(line_ending, version) -> None:
    source = _sharded_source_api(version).product_ci_source.replace(b"\r\n", b"\n").replace(b"\n", line_ending)
    expected = (producer.REQUIRED_SHARDED_PRODUCT_CI_JOB_NAMES if version == "sharded-v1"
                else producer.REQUIRED_SHARDED_V2_PRODUCT_CI_JOB_NAMES)
    assert producer.product_ci_job_policy(source) == expected


@pytest.mark.parametrize("version", ["sharded-v1", "sharded-v2", "sharded-v2-pr-cutover"])
def test_missing_gate_never_downgrades_sharded_source_to_legacy_policy(version) -> None:
    api = _sharded_source_api(version)
    api.product_ci_jobs = _SourceApi().product_ci_jobs
    with pytest.raises(producer.ContractError, match="exact required job set"):
        producer.qualify_product_ci_source(api, source_commit="b" * 40)


def test_source_policy_is_read_from_qualified_revision_not_current_workflow() -> None:
    legacy = _SourceApi()
    assert producer.qualify_product_ci_source(legacy, source_commit="b" * 40) == 43
    legacy.product_ci_source = _sharded_source_api().product_ci_source
    with pytest.raises(producer.ContractError, match="exact required job set"):
        producer.qualify_product_ci_source(legacy, source_commit="b" * 40)


@pytest.mark.parametrize("mutate", [
    lambda source: source.replace(b"sharded-v1", b"sharded-v3"),
    lambda source: source.replace(b"  CI_POLICY_VERSION: sharded-v1\n", b""),
    lambda source: source.replace(b"  required-ci-gate:\n    runs-on: ubuntu-latest\n", b""),
    lambda source: source + b"  required-ci-gate:\n    runs-on: ubuntu-latest\n",
])
def test_unknown_or_incomplete_source_policy_fails_closed(mutate) -> None:
    with pytest.raises(producer.ContractError):
        producer.product_ci_job_policy(mutate(_sharded_source_api().product_ci_source))


@pytest.mark.parametrize("version", ["sharded-v1", "sharded-v2", "sharded-v2-pr-cutover"])
def test_sharded_ci_attestation_accepts_retained_dependencies_but_not_newer_failure(version) -> None:
    api = _sharded_source_api(version)
    api.product_ci_run["run_attempt"] = 2
    rerun_job = next(job for job in api.product_ci_jobs if job["name"] == (
        "windows-full-regression" if version == "sharded-v1" else "windows-full-regression-2"
    ))
    rerun = dict(rerun_job, id=9999, run_attempt=2)
    api.product_ci_jobs.append(rerun)
    assert producer.qualify_product_ci_source(api, source_commit="b" * 40) == 43
    rerun["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="required Product CI job"):
        producer.qualify_product_ci_source(api, source_commit="b" * 40)


def _recorded_retained_ci_aliases() -> list[dict[str, Any]]:
    # Closed projection of real GitHub job history from ordinary run 37821437591
    # on be2094e (2026-10-08). Full steps are retained; console/error data is absent.
    # Core/safety were copied, whereas the failed catchup job actually reran.
    # Nonexecuted administrative skips were copied with changed start times.
    step_fields = ('name', 'number', 'status', 'conclusion', 'started_at', 'completed_at')
    recorded = [
        (
            {
                'name': 'browser-reporting-safety',
                'id': 113463183533,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'success',
                'started_at': '2026-10-08T18:04:18Z',
                'completed_at': '2026-10-08T18:04:55Z',
                'runner_id': 1000011306,
                'runner_name': 'GitHub Actions 1000011306',
                'runner_group_id': 0,
                'runner_group_name': 'GitHub Actions',
            },
            [
                ('Set up job', 1, 'completed', 'success', '2026-10-08T18:04:19Z', '2026-10-08T18:04:19Z'),
                ('Validate browser qualification request', 2, 'completed', 'success', '2026-10-08T18:04:19Z', '2026-10-08T18:04:19Z'),
                ('Checkout exact Product revision', 3, 'completed', 'success', '2026-10-08T18:04:19Z', '2026-10-08T18:04:21Z'),
                ('Setup Node.js', 4, 'completed', 'success', '2026-10-08T18:04:21Z', '2026-10-08T18:04:22Z'),
                ('Setup Python', 5, 'completed', 'success', '2026-10-08T18:04:22Z', '2026-10-08T18:04:22Z'),
                ('Install pinned reporting dependencies', 6, 'completed', 'success', '2026-10-08T18:04:22Z', '2026-10-08T18:04:26Z'),
                ('Test restricted browser reporting', 7, 'completed', 'success', '2026-10-08T18:04:26Z', '2026-10-08T18:04:27Z'),
                ('Probe the actual pinned Playwright reporter', 8, 'completed', 'success', '2026-10-08T18:04:27Z', '2026-10-08T18:04:52Z'),
                ('Test existing browser diagnostic redaction', 9, 'completed', 'success', '2026-10-08T18:04:52Z', '2026-10-08T18:04:52Z'),
                ('Test Python session diagnostics explicitly', 10, 'completed', 'success', '2026-10-08T18:04:52Z', '2026-10-08T18:04:53Z'),
                ('Post Setup Python', 18, 'completed', 'success', '2026-10-08T18:04:53Z', '2026-10-08T18:04:53Z'),
                ('Post Setup Node.js', 19, 'completed', 'success', '2026-10-08T18:04:53Z', '2026-10-08T18:04:54Z'),
                ('Post Checkout exact Product revision', 20, 'completed', 'success', '2026-10-08T18:04:54Z', '2026-10-08T18:04:54Z'),
                ('Complete job', 21, 'completed', 'success', '2026-10-08T18:04:54Z', '2026-10-08T18:04:54Z'),
            ],
        ),
        (
            {
                'name': 'browser-e2e-core',
                'id': 113464362708,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'success',
                'started_at': '2026-10-08T18:08:16Z',
                'completed_at': '2026-10-08T18:16:07Z',
                'runner_id': 1000011356,
                'runner_name': 'GitHub Actions 1000011356',
                'runner_group_id': 0,
                'runner_group_name': 'GitHub Actions',
            },
            [
                ('Set up job', 1, 'completed', 'success', '2026-10-08T18:08:17Z', '2026-10-08T18:08:19Z'),
                ('Checkout exact Product revision', 2, 'completed', 'success', '2026-10-08T18:08:19Z', '2026-10-08T18:08:26Z'),
                ('Keep browser test caches on runner storage', 3, 'completed', 'success', '2026-10-08T18:08:26Z', '2026-10-08T18:08:26Z'),
                ('Setup Python', 4, 'completed', 'success', '2026-10-08T18:08:26Z', '2026-10-08T18:08:31Z'),
                ('Retrieve fixed SQLCipher wheel', 5, 'completed', 'success', '2026-10-08T18:08:31Z', '2026-10-08T18:08:33Z'),
                ('Verify fixed SQLCipher CI source', 6, 'completed', 'success', '2026-10-08T18:08:33Z', '2026-10-08T18:08:33Z'),
                ('Declare fixed SQLCipher wheelhouse', 7, 'completed', 'success', '2026-10-08T18:08:33Z', '2026-10-08T18:08:34Z'),
                ('Setup Node.js', 8, 'completed', 'success', '2026-10-08T18:08:34Z', '2026-10-08T18:08:42Z'),
                ('Cache Playwright Chromium', 9, 'completed', 'success', '2026-10-08T18:08:42Z', '2026-10-08T18:08:48Z'),
                ('Install pinned native Noise toolchain', 10, 'completed', 'success', '2026-10-08T18:08:48Z', '2026-10-08T18:09:00Z'),
                ('Install backend dependencies', 11, 'completed', 'success', '2026-10-08T18:09:00Z', '2026-10-08T18:09:49Z'),
                ('Install frontend dependencies', 12, 'completed', 'success', '2026-10-08T18:09:49Z', '2026-10-08T18:10:22Z'),
                ('Install extension dependencies', 13, 'completed', 'success', '2026-10-08T18:10:22Z', '2026-10-08T18:10:24Z'),
                ('Run Product #5 evidence scenarios', 14, 'completed', 'success', '2026-10-08T18:10:24Z', '2026-10-08T18:10:25Z'),
                ('Build and audit deterministic extension artifact', 15, 'completed', 'success', '2026-10-08T18:10:25Z', '2026-10-08T18:10:27Z'),
                ('Build production Bridge', 16, 'completed', 'success', '2026-10-08T18:10:27Z', '2026-10-08T18:10:56Z'),
                ('Install pinned E2E dependencies', 17, 'completed', 'success', '2026-10-08T18:10:56Z', '2026-10-08T18:10:58Z'),
                ('Install Playwright Chromium', 18, 'completed', 'skipped', '2026-10-08T18:10:58Z', '2026-10-08T18:10:58Z'),
                ('Run browser E2E and preserve output', 19, 'completed', 'success', '2026-10-08T18:10:58Z', '2026-10-08T18:15:59Z'),
                ('Seal browser producer evidence', 20, 'completed', 'success', '2026-10-08T18:15:59Z', '2026-10-08T18:15:59Z'),
                ('Retain immutable browser producer evidence', 21, 'completed', 'success', '2026-10-08T18:15:59Z', '2026-10-08T18:16:01Z'),
                ('Retain restricted browser diagnostics', 22, 'completed', 'success', '2026-10-08T18:16:01Z', '2026-10-08T18:16:02Z'),
                ('Post Cache Playwright Chromium', 41, 'completed', 'success', '2026-10-08T18:16:02Z', '2026-10-08T18:16:02Z'),
                ('Post Setup Node.js', 42, 'completed', 'success', '2026-10-08T18:16:02Z', '2026-10-08T18:16:02Z'),
                ('Post Setup Python', 43, 'completed', 'success', '2026-10-08T18:16:02Z', '2026-10-08T18:16:03Z'),
                ('Post Checkout exact Product revision', 44, 'completed', 'success', '2026-10-08T18:16:03Z', '2026-10-08T18:16:05Z'),
                ('Complete job', 45, 'completed', 'success', '2026-10-08T18:16:05Z', '2026-10-08T18:16:05Z'),
            ],
        ),
        (
            {
                'name': 'browser-e2e-catchup',
                'id': 113464362716,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'failure',
                'started_at': '2026-10-08T18:09:42Z',
                'completed_at': '2026-10-08T18:15:37Z',
                'runner_id': 1000011357,
                'runner_name': 'GitHub Actions 1000011357',
                'runner_group_id': 0,
                'runner_group_name': 'GitHub Actions',
            },
            [
                ('Set up job', 1, 'completed', 'success', '2026-10-08T18:09:43Z', '2026-10-08T18:09:45Z'),
                ('Checkout exact Product revision', 2, 'completed', 'success', '2026-10-08T18:09:45Z', '2026-10-08T18:09:56Z'),
                ('Keep browser test caches on runner storage', 3, 'completed', 'success', '2026-10-08T18:09:56Z', '2026-10-08T18:09:57Z'),
                ('Setup Python', 4, 'completed', 'success', '2026-10-08T18:09:57Z', '2026-10-08T18:10:11Z'),
                ('Retrieve fixed SQLCipher wheel', 5, 'completed', 'success', '2026-10-08T18:10:11Z', '2026-10-08T18:10:12Z'),
                ('Verify fixed SQLCipher CI source', 6, 'completed', 'success', '2026-10-08T18:10:12Z', '2026-10-08T18:10:12Z'),
                ('Declare fixed SQLCipher wheelhouse', 7, 'completed', 'success', '2026-10-08T18:10:12Z', '2026-10-08T18:10:13Z'),
                ('Setup Node.js', 8, 'completed', 'success', '2026-10-08T18:10:13Z', '2026-10-08T18:10:42Z'),
                ('Cache Playwright Chromium', 9, 'completed', 'success', '2026-10-08T18:10:42Z', '2026-10-08T18:10:47Z'),
                ('Install pinned native Noise toolchain', 10, 'completed', 'success', '2026-10-08T18:10:47Z', '2026-10-08T18:11:02Z'),
                ('Install backend dependencies', 11, 'completed', 'success', '2026-10-08T18:11:02Z', '2026-10-08T18:12:16Z'),
                ('Install frontend dependencies', 12, 'completed', 'success', '2026-10-08T18:12:16Z', '2026-10-08T18:12:57Z'),
                ('Install extension dependencies', 13, 'completed', 'success', '2026-10-08T18:12:57Z', '2026-10-08T18:12:59Z'),
                ('Run Product #5 evidence scenarios', 14, 'completed', 'skipped', '2026-10-08T18:12:59Z', '2026-10-08T18:12:59Z'),
                ('Build and audit deterministic extension artifact', 15, 'completed', 'success', '2026-10-08T18:12:59Z', '2026-10-08T18:13:00Z'),
                ('Build production Bridge', 16, 'completed', 'success', '2026-10-08T18:13:00Z', '2026-10-08T18:13:21Z'),
                ('Install pinned E2E dependencies', 17, 'completed', 'success', '2026-10-08T18:13:21Z', '2026-10-08T18:13:22Z'),
                ('Install Playwright Chromium', 18, 'completed', 'skipped', '2026-10-08T18:13:22Z', '2026-10-08T18:13:22Z'),
                ('Run browser E2E and preserve output', 19, 'completed', 'failure', '2026-10-08T18:13:22Z', '2026-10-08T18:15:32Z'),
                ('Seal browser producer evidence', 20, 'completed', 'failure', '2026-10-08T18:15:32Z', '2026-10-08T18:15:32Z'),
                ('Retain immutable browser producer evidence', 21, 'completed', 'skipped', '2026-10-08T18:15:32Z', '2026-10-08T18:15:32Z'),
                ('Retain restricted browser diagnostics', 22, 'completed', 'success', '2026-10-08T18:15:32Z', '2026-10-08T18:15:33Z'),
                ('Post Cache Playwright Chromium', 41, 'completed', 'skipped', '2026-10-08T18:15:33Z', '2026-10-08T18:15:33Z'),
                ('Post Setup Node.js', 42, 'completed', 'skipped', '2026-10-08T18:15:33Z', '2026-10-08T18:15:33Z'),
                ('Post Setup Python', 43, 'completed', 'skipped', '2026-10-08T18:15:33Z', '2026-10-08T18:15:33Z'),
                ('Post Checkout exact Product revision', 44, 'completed', 'success', '2026-10-08T18:15:33Z', '2026-10-08T18:15:35Z'),
                ('Complete job', 45, 'completed', 'success', '2026-10-08T18:15:35Z', '2026-10-08T18:15:35Z'),
            ],
        ),
        (
            {
                'name': 'analytics-scale-qualification',
                'id': 113464364481,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:06:57Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
        (
            {
                'name': 'windows-full-regression-${{ matrix.shard }}',
                'id': 113464365554,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:06:57Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
        (
            {
                'name': 'browser-e2e-serial-control',
                'id': 113464365581,
                'run_id': 37821437591,
                'run_attempt': 1,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:06:57Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
        (
            {
                'name': 'browser-e2e-catchup',
                'id': 113472782770,
                'run_id': 37821437591,
                'run_attempt': 2,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'success',
                'started_at': '2026-10-08T18:26:04Z',
                'completed_at': '2026-10-08T18:35:08Z',
                'runner_id': 1000011397,
                'runner_name': 'GitHub Actions 1000011397',
                'runner_group_id': 0,
                'runner_group_name': 'GitHub Actions',
            },
            [
                ('Set up job', 1, 'completed', 'success', '2026-10-08T18:26:05Z', '2026-10-08T18:26:07Z'),
                ('Checkout exact Product revision', 2, 'completed', 'success', '2026-10-08T18:26:07Z', '2026-10-08T18:26:13Z'),
                ('Keep browser test caches on runner storage', 3, 'completed', 'success', '2026-10-08T18:26:13Z', '2026-10-08T18:26:14Z'),
                ('Setup Python', 4, 'completed', 'success', '2026-10-08T18:26:14Z', '2026-10-08T18:26:19Z'),
                ('Retrieve fixed SQLCipher wheel', 5, 'completed', 'success', '2026-10-08T18:26:19Z', '2026-10-08T18:26:20Z'),
                ('Verify fixed SQLCipher CI source', 6, 'completed', 'success', '2026-10-08T18:26:20Z', '2026-10-08T18:26:20Z'),
                ('Declare fixed SQLCipher wheelhouse', 7, 'completed', 'success', '2026-10-08T18:26:20Z', '2026-10-08T18:26:21Z'),
                ('Setup Node.js', 8, 'completed', 'success', '2026-10-08T18:26:21Z', '2026-10-08T18:26:28Z'),
                ('Cache Playwright Chromium', 9, 'completed', 'success', '2026-10-08T18:26:28Z', '2026-10-08T18:26:33Z'),
                ('Install pinned native Noise toolchain', 10, 'completed', 'success', '2026-10-08T18:26:33Z', '2026-10-08T18:26:44Z'),
                ('Install backend dependencies', 11, 'completed', 'success', '2026-10-08T18:26:44Z', '2026-10-08T18:27:33Z'),
                ('Install frontend dependencies', 12, 'completed', 'success', '2026-10-08T18:27:33Z', '2026-10-08T18:28:11Z'),
                ('Install extension dependencies', 13, 'completed', 'success', '2026-10-08T18:28:11Z', '2026-10-08T18:28:13Z'),
                ('Run Product #5 evidence scenarios', 14, 'completed', 'skipped', '2026-10-08T18:28:13Z', '2026-10-08T18:28:13Z'),
                ('Build and audit deterministic extension artifact', 15, 'completed', 'success', '2026-10-08T18:28:13Z', '2026-10-08T18:28:15Z'),
                ('Build production Bridge', 16, 'completed', 'success', '2026-10-08T18:28:15Z', '2026-10-08T18:28:44Z'),
                ('Install pinned E2E dependencies', 17, 'completed', 'success', '2026-10-08T18:28:44Z', '2026-10-08T18:28:46Z'),
                ('Install Playwright Chromium', 18, 'completed', 'skipped', '2026-10-08T18:28:46Z', '2026-10-08T18:28:46Z'),
                ('Run browser E2E and preserve output', 19, 'completed', 'success', '2026-10-08T18:28:46Z', '2026-10-08T18:34:21Z'),
                ('Seal browser producer evidence', 20, 'completed', 'success', '2026-10-08T18:34:21Z', '2026-10-08T18:34:21Z'),
                ('Retain immutable browser producer evidence', 21, 'completed', 'success', '2026-10-08T18:34:21Z', '2026-10-08T18:34:23Z'),
                ('Retain restricted browser diagnostics', 22, 'completed', 'success', '2026-10-08T18:34:23Z', '2026-10-08T18:34:24Z'),
                ('Post Cache Playwright Chromium', 41, 'completed', 'success', '2026-10-08T18:34:24Z', '2026-10-08T18:34:24Z'),
                ('Post Setup Node.js', 42, 'completed', 'success', '2026-10-08T18:34:24Z', '2026-10-08T18:34:25Z'),
                ('Post Setup Python', 43, 'completed', 'success', '2026-10-08T18:34:25Z', '2026-10-08T18:34:25Z'),
                ('Post Checkout exact Product revision', 44, 'completed', 'success', '2026-10-08T18:34:25Z', '2026-10-08T18:34:27Z'),
                ('Complete job', 45, 'completed', 'success', '2026-10-08T18:34:27Z', '2026-10-08T18:34:27Z'),
            ],
        ),
        (
            {
                'name': 'browser-e2e-serial-control',
                'id': 113472784290,
                'run_id': 37821437591,
                'run_attempt': 2,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:26:02Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
        (
            {
                'name': 'analytics-scale-qualification',
                'id': 113472784632,
                'run_id': 37821437591,
                'run_attempt': 2,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:26:02Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
        (
            {
                'name': 'windows-full-regression-${{ matrix.shard }}',
                'id': 113472785205,
                'run_id': 37821437591,
                'run_attempt': 2,
                'head_sha': 'be2094e9aa174c8dea18752315da8dc7bc4eca82',
                'status': 'completed',
                'conclusion': 'skipped',
                'started_at': '2026-10-08T18:26:02Z',
                'completed_at': '2026-10-08T18:06:57Z',
                'runner_id': None,
                'runner_name': None,
                'runner_group_id': None,
                'runner_group_name': None,
            },
            [
            ],
        ),
    ]
    jobs = [{**job, "steps": [dict(zip(step_fields, values)) for values in steps]}
            for job, steps in recorded]
    retained_aliases = [
        ('browser-e2e-core', {'id': 113472839773, 'run_attempt': 2, 'runner_group_id': None}),
        ('browser-reporting-safety', {'id': 113472847970, 'run_attempt': 2, 'runner_group_id': None}),
    ]
    for name, metadata in retained_aliases:
        original = next(job for job in jobs if job["name"] == name)
        jobs.append({**copy.deepcopy(original), **metadata})
    return jobs

class _RetainedCiJobsApi:
    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self.jobs = jobs

    def get(self, path: str) -> Any:
        assert path == (f"/repos/{producer.PRODUCT_REPOSITORY}/actions/runs/37821437591/jobs"
                        "?filter=all&per_page=100&page=1")
        return {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}


def _resolve_retained_ci_jobs(jobs: list[dict[str, Any]], attempt: int = 2) -> dict[str, dict[str, Any]]:
    return producer.latest_ci_jobs(
        _RetainedCiJobsApi(jobs), run_id=37821437591, run_attempt=attempt,
        source_commit="be2094e9aa174c8dea18752315da8dc7bc4eca82",
    )


@pytest.mark.parametrize("reverse_history", [False, True])
def test_latest_ci_jobs_resolves_recorded_retained_aliases_to_original_producers(reverse_history) -> None:
    history = _recorded_retained_ci_aliases()
    resolved = _resolve_retained_ci_jobs(history[::-1] if reverse_history else history)
    assert set(resolved) == {
        "browser-e2e-core", "browser-e2e-catchup", "browser-reporting-safety",
        "browser-e2e-serial-control", "analytics-scale-qualification",
        "windows-full-regression-${{ matrix.shard }}",
    }
    for name, original_id, step_count in (
        ("browser-e2e-core", 113464362708, 27),
        ("browser-reporting-safety", 113463183533, 14),
    ):
        original = next(job for job in history if job["name"] == name and job["run_attempt"] == 1)
        assert resolved[name] == original
        assert resolved[name]["id"] == original_id and resolved[name]["run_attempt"] == 1
        assert len(resolved[name]["steps"]) == step_count
    assert any(step["conclusion"] == "skipped" for step in resolved["browser-e2e-core"]["steps"])
    assert resolved["browser-e2e-catchup"] == next(
        job for job in history if job["name"] == "browser-e2e-catchup" and job["run_attempt"] == 2
    )
    assert resolved["browser-e2e-catchup"]["id"] == 113472782770


def _later_ci_execution(original: dict[str, Any], *, attempt: int, job_id: int) -> dict[str, Any]:
    execution = copy.deepcopy(original)
    execution.update(id=job_id, run_attempt=attempt)
    offset = timedelta(hours=attempt)
    for record in [execution, *execution["steps"]]:
        for field in ("started_at", "completed_at"):
            record[field] = (datetime.strptime(record[field], "%Y-%m-%dT%H:%M:%SZ") + offset).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    return execution


@pytest.mark.parametrize("scenario", [
    "alias-chain", "failure-then-old-alias", "success-then-old-alias", "failure-then-real-success",
])
def test_latest_ci_jobs_does_not_resurrect_old_success_after_new_execution(scenario) -> None:
    original = next(job for job in _recorded_retained_ci_aliases() if job["name"] == "browser-e2e-core")
    retained = copy.deepcopy(original)
    retained.update(id=113472839773, run_attempt=2, runner_group_id=1)
    alias_three = copy.deepcopy(retained)
    alias_three.update(id=113472839774, run_attempt=3)
    if scenario == "alias-chain":
        history, expected = [original, retained, alias_three], original
    else:
        second = _later_ci_execution(original, attempt=2, job_id=113472839775)
        if scenario != "success-then-old-alias":
            second["conclusion"] = "failure"
            second["steps"][-1]["conclusion"] = "failure"
        if scenario == "failure-then-real-success":
            third = _later_ci_execution(original, attempt=3, job_id=113472839776)
            history, expected = [original, second, third], third
        else:
            history, expected = [original, second, alias_three], second
    assert _resolve_retained_ci_jobs(history[::-1], attempt=3)[original["name"]] == expected


@pytest.mark.parametrize("same_execution_fields", [False, True])
@pytest.mark.parametrize("status,conclusion", [
    ("completed", "failure"), ("completed", "cancelled"), ("completed", "timed_out"),
    ("completed", "skipped"), ("in_progress", None), ("queued", None),
])
def test_latest_ci_jobs_preserves_newer_unsuccessful_execution(status, conclusion, same_execution_fields) -> None:
    original = next(job for job in _recorded_retained_ci_aliases() if job["name"] == "browser-e2e-core")
    if same_execution_fields:
        newer = copy.deepcopy(original)
        newer.update(run_attempt=2, id=113472839777)
    else:
        newer = _later_ci_execution(original, attempt=2, job_id=113472839777)
    newer.update(status=status, conclusion=conclusion)
    if status != "completed":
        newer["completed_at"] = None
    assert _resolve_retained_ci_jobs([newer, original])[original["name"]] == newer


@pytest.mark.parametrize("mutation", [
    "runner-id", "runner-name", "start", "end", "missing-start", "missing-end", "missing-runner-id", "missing-runner-name",
    "missing-steps", "empty-steps", "step-name", "step-number", "step-status", "step-conclusion",
    "step-start", "step-end", "missing-step-field", "extra-step-field", "reordered-steps",
    "empty-step-name", "boolean-step-number", "invalid-step-time", "invalid-job-time",
])
def test_latest_ci_jobs_refuses_contradictory_retained_execution_evidence(mutation) -> None:
    original, alias = [job for job in _recorded_retained_ci_aliases() if job["name"] == "browser-e2e-core"]
    if mutation == "runner-id":
        alias["runner_id"] += 1
    elif mutation == "runner-name":
        alias["runner_name"] = "different-runner"
    elif mutation == "start":
        alias["started_at"] = "2026-10-08T18:08:15Z"
    elif mutation == "end":
        alias["completed_at"] = "2026-10-08T18:16:08Z"
    elif mutation.startswith("missing-") and mutation != "missing-step-field":
        field = {"start": "started_at", "end": "completed_at"}.get(
            mutation.removeprefix("missing-"), mutation.removeprefix("missing-").replace("-", "_")
        )
        del alias[field]
    elif mutation == "empty-steps":
        alias["steps"] = []
    elif mutation == "step-name":
        alias["steps"][0]["name"] = "different-step"
    elif mutation == "step-number":
        alias["steps"][1]["number"] = alias["steps"][0]["number"]
    elif mutation == "step-status":
        alias["steps"][0]["status"] = "in_progress"
    elif mutation == "step-conclusion":
        alias["steps"][0]["conclusion"] = "failure"
    elif mutation in {"step-start", "step-end"}:
        alias["steps"][0]["started_at" if mutation == "step-start" else "completed_at"] = "2026-10-08T18:08:14Z"
    elif mutation == "missing-step-field":
        del alias["steps"][0]["started_at"]
    elif mutation == "extra-step-field":
        alias["steps"][0]["unexpected"] = True
    elif mutation == "reordered-steps":
        alias["steps"].reverse()
    elif mutation == "empty-step-name":
        alias["steps"][0]["name"] = ""
    elif mutation == "boolean-step-number":
        alias["steps"][0]["number"] = True
    elif mutation == "invalid-step-time":
        alias["steps"][0]["started_at"] = "invalid"
    else:
        assert mutation == "invalid-job-time"
        alias["started_at"] = "2026-02-30T18:08:16Z"
    with pytest.raises(producer.ContractError, match="retained Product CI"):
        _resolve_retained_ci_jobs([original, alias])


def test_latest_ci_jobs_does_not_infer_retention_when_complete_execution_evidence_is_absent() -> None:
    original, alias = [job for job in _recorded_retained_ci_aliases() if job["name"] == "browser-e2e-core"]
    for field in ("started_at", "completed_at", "runner_id", "runner_name", "steps"):
        original.pop(field)
        alias.pop(field)
    assert _resolve_retained_ci_jobs([original, alias])[original["name"]] == alias


@pytest.mark.parametrize("name", [
    "browser-e2e-serial-control", "analytics-scale-qualification", "windows-full-regression-${{ matrix.shard }}",
])
def test_latest_ci_jobs_preserves_recorded_nonexecuted_skips_without_producer_authority(name) -> None:
    previous, current = [job for job in _recorded_retained_ci_aliases() if job["name"] == name]
    assert previous["conclusion"] == current["conclusion"] == "skipped"
    assert previous["steps"] == current["steps"] == []
    assert previous["completed_at"] == current["completed_at"]
    assert current["started_at"] > current["completed_at"]
    assert _resolve_retained_ci_jobs([previous, current])[name] == current


def test_latest_ci_jobs_refuses_success_alias_of_a_failed_execution() -> None:
    original, alias = [job for job in _recorded_retained_ci_aliases() if job["name"] == "browser-e2e-core"]
    original["conclusion"] = "failure"
    original["steps"][-1]["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="contradictory outcomes"):
        _resolve_retained_ci_jobs([original, alias])


@pytest.mark.parametrize("lane", ["windows-full-regression-1", "windows-full-regression-2", "windows-full-regression"])
@pytest.mark.parametrize("failure", ["missing", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("version", ["sharded-v2", "sharded-v2-pr-cutover"])
def test_v2_attestation_requires_each_windows_shard_and_aggregate(lane, failure, version) -> None:
    api = _sharded_source_api(version)
    if failure == "missing":
        api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != lane]
    else:
        next(job for job in api.product_ci_jobs if job["name"] == lane)["conclusion"] = failure
    with pytest.raises(producer.ContractError):
        producer.qualify_product_ci_source(api, source_commit="b" * 40)


@pytest.mark.parametrize("source_version,job_version", [("sharded-v1", "sharded-v2"), ("sharded-v2", "sharded-v1")])
def test_sharded_policies_never_accept_each_others_actual_job_set(source_version, job_version) -> None:
    api = _sharded_source_api(source_version)
    api.product_ci_jobs = _sharded_source_api(job_version).product_ci_jobs
    with pytest.raises(producer.ContractError, match="exact required job set"):
        producer.qualify_product_ci_source(api, source_commit="b" * 40)


@pytest.mark.parametrize("old,new", [
    ("    name: windows-full-regression-${{ matrix.shard }}", "    name: windows-full-regression"),
    ("    runs-on: windows-latest", "    runs-on: ubuntu-latest"),
    ("    timeout-minutes: 60", "    timeout-minutes: 120"),
    ("    timeout-minutes: 60", "    timeout-minutes: 60\n    continue-on-error: true"),
    ("    timeout-minutes: 60", "    timeout-minutes: 60\n    if: false"),
    ("      fail-fast: false", "      fail-fast: true"),
    ("      max-parallel: 2", "      max-parallel: 3"),
    ("          - 2", "          - 1"),
    ("          - 2", "          - 3"),
    ("          - 2", "          - 2\n        exclude:\n          - shard: 2"),
    ("          - 2", "          - 2\n        include:\n          - shard: 3"),
    ("      matrix:", "      matrix:\n        os: [windows-latest]"),
    ("    strategy:", "    strategy: {}\n    strategy:"),
    ("    runs-on: windows-latest", "    runs-on: windows-latest\n    'runs-on': ubuntu-latest"),
    ("    runs-on: windows-latest", "    runs-on: windows-latest\n    <<: *other-job"),
])
def test_v2_source_refuses_incomplete_or_ambiguous_windows_matrix(old, new) -> None:
    source = _sharded_source_api("sharded-v2").product_ci_source.decode().replace("\r\n", "\n")
    before, body = source.split("  windows-full-shards:\n", 1)
    matrix, after = body.split("  windows-full-regression:\n", 1)
    assert old in matrix
    source = before + "  windows-full-shards:\n" + matrix.replace(old, new, 1) + "  windows-full-regression:\n" + after
    with pytest.raises(producer.ContractError):
        producer.product_ci_job_policy(source.encode())


@pytest.mark.parametrize("old,new", [
    ("    needs: windows-full-shards", "    needs: fixed-sqlcipher-wheel"),
    ("  windows-full-regression:\n    if: ${{ always() }}", "  windows-full-regression:\n    if: false"),
    ("      - windows-full-shards\n", ""),
    ("      - windows-full-regression\n", ""),
    ("    name: Required CI", "    name: Required CI\n    continue-on-error: true"),
])
def test_v2_source_requires_blocking_aggregate_and_both_gate_dependencies(old, new) -> None:
    source = _sharded_source_api("sharded-v2").product_ci_source.decode().replace("\r\n", "\n")
    assert old in source
    with pytest.raises(producer.ContractError):
        producer.product_ci_job_policy(source.replace(old, new, 1).encode())


@pytest.mark.parametrize("old,new", [
    ("github.event_name == 'push' || github.event_name == 'workflow_dispatch'", "github.event_name != 'pull_request'"),
    ("github.event_name == 'push' || github.event_name == 'workflow_dispatch'", "false"),
    ("pull_request) test '${{ needs.windows-full-shards.result }}' = 'skipped'", "pull_request) true"),
    ("push|workflow_dispatch) test '${{ needs.windows-full-shards.result }}' = 'success'", "push|workflow_dispatch) true"),
    ("*) echo \"Unsupported Product CI event\"; exit 1", "*) true"),
    ("  workflow_dispatch: {}", "  schedule:\n    - cron: 17 3 * * *\n  workflow_dispatch: {}"),
    ("  windows-platform-contract:\n", "  windows-platform-contract:\n    if: false\n"),
    ("  analytics-windows-contract:\n", "  analytics-windows-contract:\n    continue-on-error: true\n"),
    ("  windows-browser-e2e:\n", "  windows-browser-e2e:\n    if: false\n"),
    ("      - main\n", "      - other\n"),
])
def test_cutover_source_refuses_optional_main_coverage_or_focused_pr_jobs(old, new) -> None:
    source = _sharded_source_api("sharded-v2-pr-cutover").product_ci_source.decode().replace("\r\n", "\n")
    assert old in source
    with pytest.raises(producer.ContractError):
        producer.product_ci_job_policy(source.replace(old, new, 1).encode())


def test_old_v2_policy_cannot_authorize_the_new_pr_skip_condition() -> None:
    source = _sharded_source_api("sharded-v2-pr-cutover").product_ci_source
    with pytest.raises(producer.ContractError):
        producer.product_ci_job_policy(source.replace(b"sharded-v2-pr-cutover", b"sharded-v2"))


def test_source_commit_must_be_strictly_post_baseline() -> None:
    api = _SourceApi()
    api.set_source_commit("a" * 40)
    with pytest.raises(producer.ContractError, match="strict descendant"):
        producer.qualify_windows_package_source(
            api,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )


def test_runtime_baseline_is_required_and_has_no_embedded_default(monkeypatch) -> None:
    monkeypatch.delenv("PRODUCER_CONTROL_BASELINE_SHA", raising=False)
    with pytest.raises(
        producer.ContractError,
        match="required environment variable PRODUCER_CONTROL_BASELINE_SHA is empty",
    ):
        producer.load_producer_control_baseline()

    monkeypatch.setenv("PRODUCER_CONTROL_BASELINE_SHA", "a" * 40)
    assert producer.load_producer_control_baseline() == "a" * 40


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda api: api.run.__setitem__(
                "head_repository", {"full_name": "other-owner/product"}
            ),
            "wrong repository",
        ),
        (
            lambda api: api.workflow.__setitem__(
                "path", ".github/workflows/other.yml"
            ),
            "numeric identity or path mismatch",
        ),
        (lambda api: setattr(api, "tag_commit", "e" * 40), "does not point"),
        (
            lambda api: setattr(api, "comparison_status", "diverged"),
            "ancestor check failed",
        ),
        (lambda api: api.artifact.__setitem__("expired", True), "expired"),
        (lambda api: api.artifact.__setitem__("digest", "not-a-digest"), "digest"),
    ],
)
def test_source_qualification_rejects_identity_expiry_and_prebaseline_inputs(
    mutate, message: str
) -> None:
    api = _SourceApi()
    mutate(api)
    with pytest.raises(producer.ContractError, match=message):
        producer.qualify_windows_package_source(
            api,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )


def test_producer_must_run_from_current_main_commit(monkeypatch) -> None:
    class Api:
        def get(self, path: str) -> Any:
            assert path.endswith("/git/ref/heads/main")
            return {"object": {"sha": "a" * 40}}

    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("PRODUCER_WORKFLOW_SHA", "a" * 40)
    producer.require_current_producer_ref(Api())

    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature")
    with pytest.raises(producer.ContractError, match="dispatched from"):
        producer.require_current_producer_ref(Api())

    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    with pytest.raises(producer.ContractError, match="current Product main"):
        producer.require_current_producer_ref(Api())


def test_review_configuration_has_no_defaults_and_rejects_unsafe_paths(
    monkeypatch,
) -> None:
    names = {
        "REVIEW_REPOSITORY": REVIEW_CONFIGURATION.repository,
        "REVIEW_DEFAULT_BRANCH": REVIEW_CONFIGURATION.default_branch,
        "REVIEW_PROJECTION_PATH": REVIEW_CONFIGURATION.projection_path,
        "REVIEW_PROJECTION_DIGEST_PATH": REVIEW_CONFIGURATION.projection_digest_path,
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(producer.ContractError, match="REVIEW_REPOSITORY"):
        producer.load_review_configuration()

    for name, value in names.items():
        monkeypatch.setenv(name, value)
    assert producer.load_review_configuration() == REVIEW_CONFIGURATION

    monkeypatch.setenv("REVIEW_PROJECTION_PATH", "../projection.json")
    with pytest.raises(producer.ContractError, match="repository-relative"):
        producer.load_review_configuration()

    monkeypatch.setenv("REVIEW_PROJECTION_PATH", REVIEW_CONFIGURATION.projection_path)
    monkeypatch.setenv("REVIEW_REPOSITORY", "review-owner/evidence\ncontrol")
    with pytest.raises(producer.ContractError, match="invalid owner or name"):
        producer.load_review_configuration()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event", "pull_request", "event"),
        ("event", "push", "event"),
        ("conclusion", "failure", "conclusion"),
        ("path", ".github/workflows/other.yml", "path"),
        ("workflow_id", 0, "workflow ID"),
        ("head_branch", "v0.7.4", "head branch"),
        ("head_branch", producer.PRODUCT_DEFAULT_BRANCH, "head branch"),
        ("head_sha", "not-a-sha", "source commit"),
    ],
)
def test_source_qualification_fails_closed_on_untrusted_run_metadata(
    field: str, value: Any, message: str
) -> None:
    api = _SourceApi()
    api.run[field] = value
    with pytest.raises(producer.ContractError, match=message):
        producer.qualify_windows_package_source(
            api,
            run_id=42,
            release_tag="v0.7.5",
            baseline_sha="a" * 40,
            workflow_sha="b" * 40,
        )


class _ProjectionApi:
    def __init__(self, source: bytes, current: bytes, *, ancestor=True) -> None:
        self.source = source
        self.current = current
        self.ancestor = ancestor
        self.source_digest = hashlib.sha256(source).hexdigest().encode("ascii") + b"\n"
        self.current_digest = hashlib.sha256(current).hexdigest().encode("ascii") + b"\n"
        self.blobs = {
            "c" * 40: self.source,
            "d" * 40: self.current,
            "e" * 40: self.source_digest,
            "f" * 40: self.current_digest,
        }
        self.source_snapshot_mode = "100644"

    def get(self, path: str) -> Any:
        if "/compare/" in path:
            return {"status": "ahead" if self.ancestor else "diverged"}
        if path.endswith("/git/commits/" + "a" * 40):
            return {"tree": {"sha": "1" * 40}}
        if path.endswith("/git/commits/" + "b" * 40):
            return {"tree": {"sha": "2" * 40}}
        if path.endswith("/git/trees/" + "1" * 40):
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "projection",
                        "type": "tree",
                        "mode": "040000",
                        "sha": "3" * 40,
                    }
                ],
            }
        if path.endswith("/git/trees/" + "2" * 40):
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "projection",
                        "type": "tree",
                        "mode": "040000",
                        "sha": "4" * 40,
                    }
                ],
            }
        if path.endswith("/git/trees/" + "3" * 40):
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "review-state.json",
                        "type": "blob",
                        "mode": self.source_snapshot_mode,
                        "sha": "c" * 40,
                    },
                    {
                        "path": "review-state.sha256",
                        "type": "blob",
                        "mode": "100644",
                        "sha": "e" * 40,
                    },
                ],
            }
        if path.endswith("/git/trees/" + "4" * 40):
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "review-state.json",
                        "type": "blob",
                        "mode": "100644",
                        "sha": "d" * 40,
                    },
                    {
                        "path": "review-state.sha256",
                        "type": "blob",
                        "mode": "100644",
                        "sha": "f" * 40,
                    },
                ],
            }
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[-1]
            data = self.blobs[sha]
            return {
                "encoding": "base64",
                "size": len(data),
                "content": base64.b64encode(data).decode(),
            }
        raise AssertionError(path)


def test_review_projection_allows_unrelated_commit_but_rejects_semantic_drift() -> None:
    source = producer.canonical_projection_bytes({"policy": {"status": "approved"}})
    unrelated = bytes(source)
    digest = producer.canonical_projection_sha256(
        producer.load_json_strict(source, label="source")
    )
    accepted = producer.validate_review_projection(
        _ProjectionApi(source, unrelated),
        configuration=REVIEW_CONFIGURATION,
        source_commit="a" * 40,
        current_commit="b" * 40,
        expected_digest=digest,
    )
    assert accepted.digest == digest

    changed = producer.canonical_projection_bytes({"policy": {"status": "changed"}})
    with pytest.raises(producer.ContractError, match="digest changed"):
        producer.validate_review_projection(
            _ProjectionApi(source, changed),
            configuration=REVIEW_CONFIGURATION,
            source_commit="a" * 40,
            current_commit="b" * 40,
            expected_digest=digest,
        )
    with pytest.raises(producer.ContractError, match="ancestor check failed"):
        producer.validate_review_projection(
            _ProjectionApi(source, unrelated, ancestor=False),
            configuration=REVIEW_CONFIGURATION,
            source_commit="a" * 40,
            current_commit="b" * 40,
            expected_digest=digest,
        )


def test_review_projection_rejects_noncanonical_or_mismatched_digest_blobs() -> None:
    canonical = producer.canonical_projection_bytes({"policy": "approved"})
    digest = hashlib.sha256(canonical).hexdigest()

    noncanonical = b'{ "policy": "approved" }'
    with pytest.raises(producer.ContractError, match="not canonical JSON"):
        producer.validate_review_projection(
            _ProjectionApi(noncanonical, noncanonical),
            configuration=REVIEW_CONFIGURATION,
            source_commit="a" * 40,
            current_commit="b" * 40,
            expected_digest=hashlib.sha256(noncanonical).hexdigest(),
        )

    api = _ProjectionApi(canonical, canonical)
    api.source_digest = b"0" * 64 + b"\n"
    api.blobs["e" * 40] = api.source_digest
    with pytest.raises(producer.ContractError, match="digest changed"):
        producer.validate_review_projection(
            api,
            configuration=REVIEW_CONFIGURATION,
            source_commit="a" * 40,
            current_commit="b" * 40,
            expected_digest=digest,
        )

    symlink = _ProjectionApi(canonical, canonical)
    symlink.source_snapshot_mode = "120000"
    with pytest.raises(producer.ContractError, match="not a regular"):
        producer.validate_review_projection(
            symlink,
            configuration=REVIEW_CONFIGURATION,
            source_commit="a" * 40,
            current_commit="b" * 40,
            expected_digest=digest,
        )


def test_app_jwt_binds_numeric_app_id_and_short_lifetime(tmp_path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    token = producer.create_app_jwt(
        12345,
        key_path,
        now=2_000_000_000,
        openssl=_openssl_executable(),
    )
    header, claims, signature = token.split(".")

    def decode(segment: str) -> bytes:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))

    assert json.loads(decode(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(decode(claims)) == {
        "exp": 2_000_000_540,
        "iat": 1_999_999_940,
        "iss": "12345",
    }
    private.public_key().verify(
        decode(signature),
        f"{header}.{claims}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_handoff_token_binds_app_installation_permissions_bot_and_sole_repo(
    tmp_path: Path,
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    permissions = {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }

    class Client:
        def __init__(self, token: str, *, api_url: str) -> None:
            self.token = token

        def get(self, path: str) -> Any:
            if path == "/app":
                return {"id": 12345, "slug": "review-evidence"}
            if path == "/app/installations/67890":
                return {
                    "id": 67890,
                    "app_id": 12345,
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": permissions,
                }
            if path == "/repos/review-owner/evidence-control":
                return {
                    "id": 24680,
                    "full_name": REVIEW_CONFIGURATION.repository,
                    "default_branch": REVIEW_CONFIGURATION.default_branch,
                }
            if path == "/installation/repositories?per_page=100":
                return {
                    "total_count": 1,
                    "repositories": [
                        {"id": 24680, "full_name": REVIEW_CONFIGURATION.repository}
                    ],
                }
            if path == "/users/review-evidence%5Bbot%5D":
                return {"id": 13579}
            raise AssertionError(path)

        def post(self, path: str, payload: dict[str, Any]) -> Any:
            assert path == "/app/installations/67890/access_tokens"
            assert payload == {
                "repository_ids": [24680],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                },
            }
            return {"token": "installation-token", "permissions": permissions}

    client, identity = producer.acquire_handoff_client(
        configuration=REVIEW_CONFIGURATION,
        app_id=12345,
        installation_id=67890,
        expected_bot_user_id=13579,
        expected_repository_id=24680,
        app_private_key_path=key_path,
        api_url="https://api.github.test",
        openssl=_openssl_executable(),
        client_factory=Client,
    )
    assert client.token == "installation-token"
    assert identity == producer.HandoffIdentity(
        app_id=12345,
        installation_id=67890,
        bot_user_id=13579,
        repository_id=24680,
        app_slug="review-evidence",
    )


@pytest.mark.parametrize(
    ("resolved_name", "resolved_branch"),
    [
        ("new-owner/evidence-control", REVIEW_CONFIGURATION.default_branch),
        (REVIEW_CONFIGURATION.repository, "renamed-default"),
    ],
)
def test_handoff_rejects_repository_or_default_branch_change(
    tmp_path: Path,
    monkeypatch,
    resolved_name: str,
    resolved_branch: str,
) -> None:
    key_path = tmp_path / "unused-app.pem"
    key_path.write_text("unused", encoding="ascii")
    monkeypatch.setattr(producer, "create_app_jwt", lambda *args, **kwargs: "jwt")

    permissions = {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }

    class Client:
        def __init__(self, token: str, *, api_url: str) -> None:
            pass

        def get(self, path: str) -> Any:
            if path == "/app":
                return {"id": 1, "slug": "review-evidence"}
            if path == "/app/installations/2":
                return {
                    "id": 2,
                    "app_id": 1,
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": permissions,
                }
            if path == "/repos/review-owner/evidence-control":
                return {
                    "id": 4,
                    "full_name": resolved_name,
                    "default_branch": resolved_branch,
                }
            raise AssertionError(path)

        def post(self, path: str, payload: dict[str, Any]) -> Any:
            return {"token": "installation-token", "permissions": permissions}

    with pytest.raises(producer.ContractError, match="repository address"):
        producer.acquire_handoff_client(
            configuration=REVIEW_CONFIGURATION,
            app_id=1,
            installation_id=2,
            expected_bot_user_id=3,
            expected_repository_id=4,
            app_private_key_path=key_path,
            api_url="https://api.github.test",
            client_factory=Client,
        )


def test_stale_app_evidence_prs_are_closed_and_their_refs_removed() -> None:
    current = "b" * 40
    stale = "a" * 40
    identity = producer.HandoffIdentity(1, 2, 3, 4, "review-evidence")

    def pull(
        number: int,
        *,
        user_id: int,
        head_sha: str,
        base_sha: str,
    ) -> dict[str, Any]:
        return {
            "number": number,
            "user": {"id": user_id},
            "head": {
                "ref": f"engineering-attestation/hash-{number}",
                "sha": head_sha,
                "repo": {"id": 4},
            },
            "base": {
                "ref": REVIEW_CONFIGURATION.default_branch,
                "sha": base_sha,
                "repo": {"id": 4},
            },
        }

    class Client:
        def __init__(self) -> None:
            self.patches: list[tuple[str, dict[str, Any]]] = []
            self.deletes: list[str] = []
            self.parents = {
                "7" * 40: stale,
                "8" * 40: current,
            }

        def get(self, path: str) -> Any:
            if "/pulls?" in path and "page=1" in path:
                return [
                    # GitHub can report the moving current base SHA for an older PR;
                    # only the evidence commit's parent records its creation base.
                    pull(7, user_id=3, head_sha="7" * 40, base_sha=current),
                    pull(8, user_id=3, head_sha="8" * 40, base_sha=current),
                    pull(9, user_id=99, head_sha="9" * 40, base_sha=current),
                ]
            prefix = "/repos/review-owner/evidence-control/git/commits/"
            assert path.startswith(prefix)
            head_sha = path.removeprefix(prefix)
            return {"parents": [{"sha": self.parents[head_sha]}]}

        def patch(self, path: str, payload: dict[str, Any]) -> Any:
            self.patches.append((path, payload))

        def delete(self, path: str) -> None:
            self.deletes.append(path)

    client = Client()
    assert producer.retire_stale_evidence_prs(
        client,
        configuration=REVIEW_CONFIGURATION,
        identity=identity,
        current_base_commit=current,
    ) == [7]
    assert client.patches == [
        (
            "/repos/review-owner/evidence-control/pulls/7",
            {"state": "closed"},
        )
    ]
    assert client.deletes == [
        "/repos/review-owner/evidence-control/git/refs/heads/"
        "engineering-attestation%2Fhash-7"
    ]


def test_evidence_pr_uses_exact_alias_blobs_and_cleans_a_changed_base() -> None:
    base_commit = "a" * 40
    identity = producer.HandoffIdentity(1, 2, 3, 4, "review-evidence")
    attestation = b'{"provenance":{"signature":"test"}}\n'
    artifact = b"exact chrome zip bytes"

    class Client:
        def __init__(
            self,
            current_base: str,
            *,
            author_id: int = 3,
            signature_verified: bool = True,
            evidence_parent: str | None = None,
        ) -> None:
            self.current_base = current_base
            self.author_id = author_id
            self.signature_verified = signature_verified
            self.evidence_parent = (
                base_commit if evidence_parent is None else evidence_parent
            )
            self.blob_calls = 0
            self.tree_payload: dict[str, Any] | None = None
            self.patches: list[tuple[str, dict[str, Any]]] = []
            self.deletes: list[str] = []
            self.created_branch: str | None = None

        def get(self, path: str) -> Any:
            if path.endswith("/git/commits/" + base_commit):
                return {"tree": {"sha": "b" * 40}}
            if path.endswith("/commits/" + "d" * 40):
                return {
                    "author": {"id": self.author_id},
                    "committer": {
                        "id": 19864447,
                        "login": "web-flow",
                    },
                    "parents": [{"sha": self.evidence_parent}],
                    "commit": {
                        "verification": {
                            "verified": self.signature_verified,
                            "reason": (
                                "valid"
                                if self.signature_verified
                                else "unsigned"
                            ),
                        }
                    },
                }
            if path.endswith("/git/ref/heads/trunk"):
                return {"object": {"sha": self.current_base}}
            raise AssertionError(path)

        def post(self, path: str, payload: dict[str, Any]) -> Any:
            if path.endswith("/git/blobs"):
                self.blob_calls += 1
                return {"sha": str(self.blob_calls) * 40}
            if path.endswith("/git/trees"):
                self.tree_payload = payload
                return {"sha": "c" * 40}
            if path.endswith("/git/commits"):
                assert payload["parents"] == [base_commit]
                return {"sha": "d" * 40}
            if path.endswith("/git/refs"):
                self.created_branch = payload["ref"].removeprefix("refs/heads/")
                return {}
            if path.endswith("/pulls"):
                assert self.created_branch is not None
                return {
                    "number": 11,
                    "html_url": "https://github.test/review/pull/11",
                    "user": {"id": 3},
                    "head": {
                        "ref": self.created_branch,
                        "sha": "d" * 40,
                        "repo": {"id": 4},
                    },
                    "base": {
                        "ref": REVIEW_CONFIGURATION.default_branch,
                        "sha": base_commit,
                        "repo": {"id": 4},
                    },
                }
            raise AssertionError(path)

        def patch(self, path: str, payload: dict[str, Any]) -> Any:
            self.patches.append((path, payload))

        def delete(self, path: str) -> None:
            self.deletes.append(path)

    client = Client(base_commit)
    assert producer.create_evidence_pr(
        client,
        configuration=REVIEW_CONFIGURATION,
        identity=identity,
        review_base_commit=base_commit,
        attestation_bytes=attestation,
        artifact_bytes=artifact,
        pr_body="audit context",
        workflow_run_id=12,
        workflow_run_attempt=1,
    ) == (11, "https://github.test/review/pull/11")

    assert client.tree_payload is not None
    tree = client.tree_payload["tree"]
    archive = hashlib.sha256(attestation).hexdigest()
    assert tree == [
        {
            "path": f"compliance/engineering/releases/attestations/{archive}/artifact.bin",
            "mode": "100644",
            "type": "blob",
            "sha": "2" * 40,
        },
        {
            "path": f"compliance/engineering/releases/attestations/{archive}/attestation.json",
            "mode": "100644",
            "type": "blob",
            "sha": "1" * 40,
        },
        {
            "path": "compliance/engineering/releases/current/artifact.bin",
            "mode": "100644",
            "type": "blob",
            "sha": "2" * 40,
        },
        {
            "path": "compliance/engineering/releases/current/attestation.json",
            "mode": "100644",
            "type": "blob",
            "sha": "1" * 40,
        },
    ]

    changed = Client("e" * 40)
    with pytest.raises(producer.ContractError, match="advanced"):
        producer.create_evidence_pr(
            changed,
            configuration=REVIEW_CONFIGURATION,
            identity=identity,
            review_base_commit=base_commit,
            attestation_bytes=attestation,
            artifact_bytes=artifact,
            pr_body="audit context",
            workflow_run_id=12,
            workflow_run_attempt=2,
        )
    assert changed.patches == [
        (
            "/repos/review-owner/evidence-control/pulls/11",
            {"state": "closed"},
        )
    ]
    assert len(changed.deletes) == 1

    wrong_author = Client(base_commit, author_id=99)

    with pytest.raises(
        producer.ContractError,
        match="App author, signature, or parent",
    ):
        producer.create_evidence_pr(
            wrong_author,
            configuration=REVIEW_CONFIGURATION,
            identity=identity,
            review_base_commit=base_commit,
            attestation_bytes=attestation,
            artifact_bytes=artifact,
            pr_body="audit context",
            workflow_run_id=12,
            workflow_run_attempt=3,
        )

    invalid_signature = Client(
        base_commit,
        signature_verified=False,
    )

    with pytest.raises(
        producer.ContractError,
        match="App author, signature, or parent",
    ):
        producer.create_evidence_pr(
            invalid_signature,
            configuration=REVIEW_CONFIGURATION,
            identity=identity,
            review_base_commit=base_commit,
            attestation_bytes=attestation,
            artifact_bytes=artifact,
            pr_body="audit context",
            workflow_run_id=12,
            workflow_run_attempt=4,
        )

    wrong_parent = Client(
        base_commit,
        evidence_parent="f" * 40,
    )

    with pytest.raises(
        producer.ContractError,
        match="App author, signature, or parent",
    ):
        producer.create_evidence_pr(
            wrong_parent,
            configuration=REVIEW_CONFIGURATION,
            identity=identity,
            review_base_commit=base_commit,
            attestation_bytes=attestation,
            artifact_bytes=artifact,
            pr_body="audit context",
            workflow_run_id=12,
            workflow_run_attempt=5,
        )


def test_handoff_rejects_app_permission_expansion(tmp_path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Client:
        def __init__(self, token: str, *, api_url: str) -> None:
            pass

        def get(self, path: str) -> Any:
            if path == "/app":
                return {"id": 1, "slug": "review-evidence"}
            if path == "/app/installations/2":
                return {
                    "id": 2,
                    "app_id": 1,
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": {
                        "administration": "write",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                    },
                }
            raise AssertionError(path)

    with pytest.raises(producer.ContractError, match="permissions exceed"):
        producer.acquire_handoff_client(
            configuration=REVIEW_CONFIGURATION,
            app_id=1,
            installation_id=2,
            expected_bot_user_id=3,
            expected_repository_id=4,
            app_private_key_path=key_path,
            api_url="https://api.github.test",
            openssl=_openssl_executable(),
            client_factory=Client,
        )
