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
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from tools import engineering_attestation as producer


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
) -> tuple[str, bytes]:
    manifest = {
        "key": MANIFEST_KEY,
        "manifest_version": 3,
        "version": "2.0.0",
    }
    outputs = {
        "background.js": b"export const ready = true;\n",
        "manifest.json": (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    }
    metadata = {
        "schema": "ofca-extension-build/v3",
        "extension_version": "2.0.0",
        "extension_id": producer.EXPECTED_EXTENSION_ID,
        "determinism_verified": True,
        "outputs": {
            name: f"sha256:{hashlib.sha256(data).hexdigest()}"
            for name, data in outputs.items()
        },
        "target": "chrome116",
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
            "event": "push",
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
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 43,
                "head_sha": "b" * 40,
            }
            for name in sorted(producer.REQUIRED_PRODUCT_CI_JOB_NAMES)
        ]
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
        if path.endswith("/actions/runs/43/attempts/1/jobs?per_page=100"):
            return {
                "total_count": len(self.product_ci_jobs),
                "jobs": self.product_ci_jobs,
            }
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
        ("conclusion", "failure", "conclusion"),
        ("path", ".github/workflows/other.yml", "path"),
        ("workflow_id", 0, "workflow ID"),
        ("head_branch", "v0.7.4", "head branch"),
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
