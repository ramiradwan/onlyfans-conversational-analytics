"""Attestation qualifies the exact Store package for itself, or refuses.

Every test here drives the production precondition chain against a real Store
ZIP produced by the release packaging path, and a loopback stand-in for the
Legal repository the release gate reads. A refusal must name its own step and
must stop before any key material is decoded.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import struct
import subprocess
import threading
import urllib.parse
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from tools import engineering_attestation as producer


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "extension"
EXTENSION_DIST = EXTENSION_ROOT / "dist"
FIXTURES = EXTENSION_ROOT / "tests" / "fixtures"
SIGNING_RULE_FIXTURE = FIXTURES / "packaged-signing-rule.json"
LEGAL_FIXTURE = FIXTURES / "legal-instrument-bindings.synthetic.json"
PRIVACY_POLICY_URL = "https://legal-evidence.example.com/legal/privacy"

LEGAL_REPOSITORY = "test-owner/test-legal"
LEGAL_DOCUMENT_PATH = "compliance/cws/releases/2.0.1/legal-release-bindings.json"
INSTALLATION_TOKEN = "ghs-synthetic-installation-token"
# Revision B is deliberately not revision A. The Legal contract forbids Product
# requiring them to agree, so the chain must carry two values throughout.
FETCH_REVISION = "1f0d2c3b4a596877665544332211ffeeddccbbaa"
PRODUCER_SHA = "9988776655443322110000ffeeddccbbaa998877"
BASELINE_SHA = "a" * 40
SOURCE_COMMIT = "b" * 40
INSTALLER_BYTES = b"signed-installer"


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


def _require_extension_toolchain() -> None:
    if not (EXTENSION_ROOT / "node_modules").is_dir():
        pytest.skip("extension dependencies are not installed")
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed on this test host")


def _clear_extension_archives() -> None:
    for archive in EXTENSION_DIST.glob("conversation-analytics-*.zip"):
        archive.unlink()


def _build_environment() -> dict[str, str]:
    """The packaging environment CI provides.

    ZIP entries carry a naive local time, so the archive the release path
    produces is only the fixed 1980-01-01T00:00:00 the attestation requires
    when the packaging host runs in UTC, as the hosted runners do.
    """

    return os.environ | {"TZ": "UTC"}


def _run_extension_build(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(EXTENSION_ROOT / "build.mjs"), *arguments],
        cwd=EXTENSION_ROOT,
        env=_build_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def packaged_release(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One real development build and one real Store package, built in order.

    The development build runs first so the extension tree is left in the
    release state the packaging tests also leave it in.
    """

    _require_extension_toolchain()
    workspace = tmp_path_factory.mktemp("packaged-release")
    _clear_extension_archives()

    development = _run_extension_build([])
    assert development.returncode == 0, development.stdout + development.stderr
    development_metadata = json.loads(
        (EXTENSION_DIST / "build-meta.json").read_text(encoding="utf-8")
    )

    packaged = _run_extension_build(
        [
            "--package",
            f"--packaged-signing-rule={SIGNING_RULE_FIXTURE}",
            f"--legal-release-bindings={LEGAL_FIXTURE}",
            f"--privacy-policy-url={PRIVACY_POLICY_URL}",
        ]
    )
    assert packaged.returncode == 0, packaged.stdout + packaged.stderr
    built = sorted(EXTENSION_DIST.glob("conversation-analytics-*.zip"))
    assert len(built) == 1, built

    version = json.loads(
        (EXTENSION_ROOT / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    filename = f"OnlyFans-Conversational-Analytics-Agent-{version}-chrome.zip"
    store_zip = workspace / filename
    shutil.move(str(built[0]), store_zip)
    _clear_extension_archives()

    document = LEGAL_FIXTURE.read_bytes()
    return {
        "version": version,
        "release_tag": f"v{version}",
        "filename": filename,
        "bytes": store_zip.read_bytes(),
        "development_metadata": development_metadata,
        "legal_document": document,
        "legal_digest": hashlib.sha256(document).hexdigest(),
        "legal_source_revision": json.loads(document)["legal_repository_revision"],
    }


def _actions_artifact(
    store_zip: bytes, *, filename: str, version: str
) -> tuple[bytes, str]:
    """The Actions artifact the Windows package workflow publishes."""

    installer_name = f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe"
    sums = (
        f"{hashlib.sha256(store_zip).hexdigest()} *{filename}\n"
        f"{hashlib.sha256(INSTALLER_BYTES).hexdigest()} *{installer_name}\n"
    ).encode("ascii")
    payload = io.BytesIO()
    entries = {
        filename: store_zip,
        installer_name: INSTALLER_BYTES,
        "sha256sums.txt": sums,
    }
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    raw = payload.getvalue()
    return raw, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _store_entries(store_zip: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(store_zip)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _zero_external_attributes(archive: bytes) -> bytes:
    """Clear the external attributes zipfile insists on writing.

    ``ZipFile`` substitutes 0o600 for a zeroed attribute word, which the
    release packer does not, so the field is cleared in the central directory
    afterwards. Without this a repacked archive would be refused for its shape
    and never reach the check under test.
    """

    end = archive.rindex(b"PK\x05\x06")
    size, offset = struct.unpack_from("<II", archive, end + 12)
    data = bytearray(archive)
    cursor = offset
    while cursor < offset + size:
        assert data[cursor : cursor + 4] == b"PK\x01\x02"
        name, extra, comment = struct.unpack_from("<HHH", data, cursor + 28)
        struct.pack_into("<I", data, cursor + 38, 0)
        cursor += 46 + name + extra + comment
    assert cursor == offset + size
    return bytes(data)


def _rebuild_store_zip(entries: dict[str, bytes]) -> bytes:
    """Repack entries the way the release path packs them.

    Sorted names, the fixed 1980 timestamp and zeroed attributes, so a
    repacked archive is refused for its content rather than for its shape.
    """

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])
    return _zero_external_attributes(payload.getvalue())


def _qualified_zip(
    packaged_release: dict[str, Any], *, store_zip: bytes | None = None
) -> producer.QualifiedChromeZip:
    payload = packaged_release["bytes"] if store_zip is None else store_zip
    archive, digest = _actions_artifact(
        payload,
        filename=packaged_release["filename"],
        version=packaged_release["version"],
    )
    return producer.qualify_downloaded_artifact(
        archive,
        expected_server_digest=digest,
        release_tag=packaged_release["release_tag"],
    )


def _coordinates(
    packaged_release: dict[str, Any],
    *,
    expected_digest: str | None = None,
    source_revision: str | None = None,
    fetch_revision: str | None = None,
) -> producer.LegalBindingsCoordinates:
    return producer.resolve_legal_bindings_coordinates(
        source_revision=source_revision or packaged_release["legal_source_revision"],
        fetch_revision=fetch_revision or FETCH_REVISION,
        document_path=LEGAL_DOCUMENT_PATH,
        expected_digest=expected_digest or packaged_release["legal_digest"],
    )


@functools.cache
def _synthetic_signing_key() -> str:
    """A per-run key. The gate signs a real assertion, so it needs a real key,
    and no credential of any kind is checked in for it."""

    minted = subprocess.run(
        [
            "node",
            "-e",
            "const {generateKeyPairSync}=require('node:crypto');"
            "const {privateKey}=generateKeyPairSync('rsa',{modulusLength:2048});"
            "process.stdout.write(privateKey.export({type:'pkcs8',format:'pem'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return base64.b64encode(minted.stdout.encode("ascii")).decode("ascii")


class _LegalRepository:
    """A loopback stand-in for the Legal repository routes the gate reads."""

    def __init__(self, document: bytes, source_revision: str) -> None:
        self.document = document
        self.source_revision = source_revision
        self.document_requests = 0


@contextmanager
def _legal_repository(document: bytes, source_revision: str) -> Iterator[
    tuple[str, _LegalRepository]
]:
    state = _LegalRepository(document, source_revision)
    contents_path = f"/repos/{LEGAL_REPOSITORY}/contents/{LEGAL_DOCUMENT_PATH}"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server's interface
            if self.path.endswith("/access_tokens"):
                body = json.dumps({"token": INSTALLATION_TOKEN}).encode()
                self._respond(201, body)
                return
            self._respond(404, b"{}")

        def do_GET(self) -> None:  # noqa: N802 - http.server's interface
            path, _, query = self.path.partition("?")
            commit_route = (
                f"/repos/{LEGAL_REPOSITORY}/commits/{state.source_revision}"
            )
            if path == commit_route:
                body = json.dumps({"sha": state.source_revision}).encode()
                self._respond(200, body)
                return
            authorized = (
                self.headers.get("authorization") == f"Bearer {INSTALLATION_TOKEN}"
            )
            if authorized and path == contents_path and query == f"ref={FETCH_REVISION}":
                state.document_requests += 1
                self._respond(200, state.document)
                return
            self._respond(404, b"{}")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _gate_environment(base_url: str) -> dict[str, str]:
    environment = os.environ | {
        "PRODUCT_REVISION": PRODUCER_SHA,
        "GITHUB_SHA": PRODUCER_SHA,
        "LEGAL_BINDINGS_API_BASE_URL": base_url,
        "LEGAL_BINDINGS_REPOSITORY": LEGAL_REPOSITORY,
        "LEGAL_BINDINGS_APP_ID": "1234",
        "LEGAL_BINDINGS_APP_PRIVATE_KEY_B64": _synthetic_signing_key(),
        "LEGAL_BINDINGS_INSTALLATION_ID": "424242",
    }
    environment.pop("GITHUB_WORKSPACE", None)
    return environment


def test_every_precondition_refuses_with_its_own_exit_code() -> None:
    """A shared exit code would make two refusals indistinguishable."""

    codes = producer.QUALIFICATION_EXIT_CODES
    assert len(set(codes.values())) == len(codes)
    assert set(codes) == {
        producer.STEP_LEGAL_COORDINATES,
        producer.STEP_LEGAL_RETRIEVAL,
        producer.STEP_LEGAL_DIGEST,
        producer.STEP_SIGNING_RULE,
        producer.STEP_PRIVACY_POLICY,
        producer.STEP_PACKAGE_AUDIT,
        producer.STEP_ARTIFACT_DIGEST,
        producer.STEP_PACKAGE_QUALIFICATION,
    }
    # 0 and 1 are success and an unhandled error; 2 is a plain contract refusal.
    assert min(codes.values()) > 2


def test_the_exact_store_package_qualifies_end_to_end(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The positive control: the whole chain runs, so its refusals mean something."""

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        release = producer.qualify_store_release(
            chrome_zip,
            _coordinates(packaged_release),
            temporary_root=tmp_path,
            audit_package=True,
            environment=_gate_environment(base_url),
        )
    assert state.document_requests == 1
    assert release.artifact_sha256 == hashlib.sha256(
        packaged_release["bytes"]
    ).hexdigest()
    assert release.legal_bindings.digest == packaged_release["legal_digest"]
    assert release.legal_bindings.source_revision == (
        packaged_release["legal_source_revision"]
    )
    assert release.legal_bindings.fetch_revision == FETCH_REVISION
    assert release.legal_bindings.source_revision != release.legal_bindings.fetch_revision
    assert release.signing_rule.sha256 == (
        chrome_zip.metadata["signing_rule"]["sha256"]
    )
    assert release.privacy_policy.url == PRIVACY_POLICY_URL


def test_a_flipped_byte_in_the_store_package_never_reaches_the_chain(
    packaged_release: dict[str, Any],
) -> None:
    """One flipped byte in a packaged file, repacked and re-summed around it."""

    entries = _store_entries(packaged_release["bytes"])
    target = sorted(name for name in entries if name != "build-meta.json")[0]
    mutated_entry = bytearray(entries[target])
    mutated_entry[0] ^= 0x01
    entries[target] = bytes(mutated_entry)

    with pytest.raises(producer.ContractError) as refusal:
        _qualified_zip(packaged_release, store_zip=_rebuild_store_zip(entries))
    assert f"extension output digest mismatch for {target}" in str(refusal.value)
    # It is refused for its content by the artifact qualification, before any
    # Legal coordinate is read, so it is a plain contract refusal.
    assert not isinstance(refusal.value, producer.QualificationError)


def test_a_self_consistent_package_still_has_to_pass_the_audit(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """A repacked ZIP whose own metadata agrees with its mutated content.

    Nothing an upstream job could claim makes this qualify: the metadata is
    internally consistent, so only running the controlled audit against these
    exact bytes refuses it.
    """

    entries = _store_entries(packaged_release["bytes"])
    popup = entries["popup.html"].replace(
        b"Observed in this browser", b"Observed in this browsel", 1
    )
    assert popup != entries["popup.html"]
    entries["popup.html"] = popup
    metadata = json.loads(entries["build-meta.json"].decode("utf-8"))
    metadata["outputs"]["popup.html"] = f"sha256:{hashlib.sha256(popup).hexdigest()}"
    entries["build-meta.json"] = json.dumps(metadata, indent=2).encode("utf-8") + b"\n"

    chrome_zip = _qualified_zip(
        packaged_release, store_zip=_rebuild_store_zip(entries)
    )
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, _):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert refusal.value.step == producer.STEP_PACKAGE_AUDIT
    assert refusal.value.exit_code == 13


def test_a_store_zip_substituted_after_the_audit_is_refused(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The digest is recomputed over the bytes the audit ran against."""

    audited = tmp_path / packaged_release["filename"]
    audited.write_bytes(packaged_release["bytes"])
    expected = hashlib.sha256(packaged_release["bytes"]).hexdigest()
    assert producer.recompute_store_zip_digest(audited, expected=expected) == expected

    substituted = bytearray(packaged_release["bytes"])
    substituted[len(substituted) // 2] ^= 0x01
    audited.write_bytes(bytes(substituted))
    with pytest.raises(producer.QualificationError) as refusal:
        producer.recompute_store_zip_digest(audited, expected=expected)
    assert refusal.value.step == producer.STEP_ARTIFACT_DIGEST
    assert refusal.value.exit_code == 14


def test_a_document_that_cannot_be_retrieved_refuses_under_its_own_step(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """A retrieval failure stays distinguishable from a digest failure."""

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                # Revision B names a fetch revision the repository will not
                # serve the document at.
                _coordinates(packaged_release, fetch_revision="c" * 40),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert state.document_requests == 0
    assert refusal.value.step == producer.STEP_LEGAL_RETRIEVAL
    assert refusal.value.exit_code == 11


def test_a_signing_rule_the_artifact_does_not_carry_is_refused(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The packaged rule identity refuses under its own step, not the ZIP's."""

    entries = _store_entries(packaged_release["bytes"])
    rule = json.loads(entries["packaged-signing-rule.json"].decode("utf-8"))
    metadata = json.loads(entries["build-meta.json"].decode("utf-8"))
    assert metadata["signing_rule"]["schema"] == rule["schema"]
    metadata["signing_rule"]["sha256"] = "sha256:" + "f" * 64
    entries["build-meta.json"] = json.dumps(metadata, indent=2).encode("utf-8") + b"\n"

    chrome_zip = _qualified_zip(
        packaged_release, store_zip=_rebuild_store_zip(entries)
    )
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, _):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert refusal.value.step == producer.STEP_SIGNING_RULE
    assert refusal.value.exit_code == 16
    assert refusal.value.exit_code != producer.QUALIFICATION_EXIT_CODES[
        producer.STEP_ARTIFACT_DIGEST
    ]


def test_a_privacy_policy_url_outside_the_legal_instrument_is_refused(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The shipped URL must be the verified instrument's own public route."""

    entries = _store_entries(packaged_release["bytes"])
    config = json.loads(entries["extension-config.json"].decode("utf-8"))
    assert config["privacy_policy_url"] == PRIVACY_POLICY_URL
    config["privacy_policy_url"] = "https://legal-evidence.example.com/legal/other"
    replacement = json.dumps(config, indent=2).encode("utf-8") + b"\n"
    entries["extension-config.json"] = replacement
    metadata = json.loads(entries["build-meta.json"].decode("utf-8"))
    metadata["outputs"]["extension-config.json"] = (
        f"sha256:{hashlib.sha256(replacement).hexdigest()}"
    )
    entries["build-meta.json"] = json.dumps(metadata, indent=2).encode("utf-8") + b"\n"

    chrome_zip = _qualified_zip(
        packaged_release, store_zip=_rebuild_store_zip(entries)
    )
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    # The Legal document was retrieved and accepted, so the refusal is the
    # packaged URL disagreeing with it rather than a retrieval failure.
    assert state.document_requests == 1
    assert refusal.value.step == producer.STEP_PRIVACY_POLICY
    assert refusal.value.exit_code == 17


def test_a_development_artifact_can_never_qualify(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The development path records no bindings, and refuses before retrieval."""

    metadata = packaged_release["development_metadata"]
    assert metadata["legal_bindings"] is None
    assert metadata["signing_rule"] is None
    assert metadata["privacy_policy_configured"] is False

    development_zip = producer.QualifiedChromeZip(
        filename=packaged_release["filename"],
        version=packaged_release["version"],
        sha256=hashlib.sha256(packaged_release["bytes"]).hexdigest(),
        size_bytes=len(packaged_release["bytes"]),
        bytes=packaged_release["bytes"],
        actions_archive_sha256="c" * 64,
        entries={},
        metadata=metadata,
    )
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                development_zip,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert refusal.value.step == producer.STEP_LEGAL_COORDINATES
    assert refusal.value.exit_code == 10
    # No coordinate, and no retrieval, can rescue it: it refused before either.
    assert state.document_requests == 0
    # The release filename is a second, independent barrier: the development
    # bundle is not named as a Store candidate.
    assert producer.AGENT_ZIP.fullmatch(
        f"agent-development-unpacked-{packaged_release['version']}.zip"
    ) is None


def test_a_source_revision_the_artifact_was_not_packaged_against_is_refused(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """Revision A is compared against the artifact before any retrieval runs."""

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                _coordinates(packaged_release, source_revision="e" * 40),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert refusal.value.step == producer.STEP_LEGAL_COORDINATES
    assert state.document_requests == 0


def test_the_embedded_block_is_compared_against_the_recomputed_digest(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The coordinates and the retrieved document agree; the artifact does not.

    The embedded block is edited in the packaged bytes, which the build
    metadata carries no digest of, so the archive qualifies structurally and
    the recomputed digest is what refuses its claim about which Legal document
    it binds.
    """

    entries = _store_entries(packaged_release["bytes"])
    metadata = json.loads(entries["build-meta.json"].decode("utf-8"))
    assert metadata["legal_bindings"]["legal_bindings_digest"] == (
        packaged_release["legal_digest"]
    )
    metadata["legal_bindings"]["legal_bindings_digest"] = "b" * 64
    entries["build-meta.json"] = json.dumps(metadata, indent=2).encode("utf-8") + b"\n"
    disagreeing = _qualified_zip(
        packaged_release, store_zip=_rebuild_store_zip(entries)
    )

    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                disagreeing,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    # The document really was retrieved and really did match its coordinate,
    # so only the comparison against the embedded block can be refusing.
    assert state.document_requests == 1
    assert refusal.value.step == producer.STEP_LEGAL_DIGEST
    assert refusal.value.exit_code == 12
    assert "recomputed here" in str(refusal.value)


def test_the_embedded_block_is_compared_against_the_retrieved_document(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The retrieved bytes decide, and disagreement is a failure, not a warning."""

    served = json.loads(packaged_release["legal_document"])
    served["instruments"]["privacy_policy"]["version"] = "9.9.9"
    canonical = json.dumps(
        served, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() != packaged_release["legal_digest"]

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        canonical, packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_store_release(
                chrome_zip,
                _coordinates(packaged_release),
                temporary_root=tmp_path,
                audit_package=True,
                environment=_gate_environment(base_url),
            )
    assert state.document_requests == 1
    assert refusal.value.step == producer.STEP_LEGAL_DIGEST
    assert refusal.value.exit_code == 12


def test_a_duplicated_member_in_the_legal_document_is_refused(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The serializer alone cannot see a duplicate; the call site's byte
    comparison is what rejects it, so the coordinates are set to the duplicated
    bytes' own digest and canonicality is the only thing left to refuse."""

    document = packaged_release["legal_document"].decode("ascii")
    duplicated = document.replace(
        '"schema":', '"schema":"ofca-legal-instrument-bindings/v1","schema":', 1
    ).encode("ascii")
    assert json.loads(duplicated)["schema"] == "ofca-legal-instrument-bindings/v1"
    duplicated_digest = hashlib.sha256(duplicated).hexdigest()

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        duplicated, packaged_release["legal_source_revision"]
    ) as (base_url, state):
        with pytest.raises(producer.QualificationError) as refusal:
            producer.qualify_legal_bindings(
                producer.QualifiedChromeZip(
                    filename=chrome_zip.filename,
                    version=chrome_zip.version,
                    sha256=chrome_zip.sha256,
                    size_bytes=chrome_zip.size_bytes,
                    bytes=chrome_zip.bytes,
                    actions_archive_sha256=chrome_zip.actions_archive_sha256,
                    entries=chrome_zip.entries,
                    metadata=dict(chrome_zip.metadata)
                    | {
                        "legal_bindings": dict(chrome_zip.metadata["legal_bindings"])
                        | {"legal_bindings_digest": duplicated_digest}
                    },
                ),
                _coordinates(packaged_release, expected_digest=duplicated_digest),
                output=tmp_path / "legal-release-bindings.json",
                runner_temp=tmp_path,
                environment=_gate_environment(base_url),
            )
    assert state.document_requests == 1
    assert refusal.value.step == producer.STEP_LEGAL_DIGEST
    assert "not stored in its canonical form" in str(refusal.value)


def test_the_signed_payload_binds_every_release_coordinate(
    packaged_release: dict[str, Any], tmp_path: Path
) -> None:
    """The payload is signed with a key minted here and discarded with the test."""

    chrome_zip = _qualified_zip(packaged_release)
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, _):
        release = producer.qualify_store_release(
            chrome_zip,
            _coordinates(packaged_release),
            temporary_root=tmp_path,
            audit_package=True,
            environment=_gate_environment(base_url),
        )

    source = producer.QualifiedSource(
        run_id=42,
        run_attempt=2,
        product_ci_run_id=43,
        source_commit=SOURCE_COMMIT,
        artifact_id=91,
        artifact_name=f"windows-package-{packaged_release['release_tag']}",
        artifact_server_digest="sha256:" + "c" * 64,
        archive_download_url="https://api.github.com/artifact/91",
    )
    qualification = producer.PackageQualification(
        workflow=producer.PRODUCER_WORKFLOW,
        job=producer.QUALIFICATION_JOB_NAME,
        job_id=7001,
        run_id=99,
        run_attempt=1,
        conclusion="success",
    )
    projection = producer.ReviewProjection(
        source_commit="e" * 40,
        current_commit="f" * 40,
        digest="1" * 64,
        value={"state": "reviewed"},
    )
    attestation = producer.build_attestation_document(
        source=source,
        release=release,
        qualification=qualification,
        projection=projection,
        attestation_id="chrome-extension-99-1",
        signed_at="2026-01-01T00:00:00Z",
    )

    assert attestation["commit_hash"] == SOURCE_COMMIT
    assert attestation["artifact"]["filename"] == packaged_release["filename"]
    assert attestation["artifact"]["sha256"] == hashlib.sha256(
        packaged_release["bytes"]
    ).hexdigest()
    bindings = attestation["legal_bindings"]
    assert bindings["schema"] == producer.LEGAL_BINDINGS_SCHEMA
    assert bindings["legal_repository_revision"] == (
        packaged_release["legal_source_revision"]
    )
    assert bindings["legal_bindings_repository_revision"] == FETCH_REVISION
    assert bindings["legal_repository_revision"] != (
        bindings["legal_bindings_repository_revision"]
    )
    assert bindings["legal_bindings_path"] == LEGAL_DOCUMENT_PATH
    assert bindings["legal_bindings_digest"] == packaged_release["legal_digest"]
    assert attestation["packaged_signing_rule"]["sha256"] == (
        chrome_zip.metadata["signing_rule"]["sha256"]
    )
    assert attestation["release_privacy_policy"]["url"] == PRIVACY_POLICY_URL
    assert attestation["package_qualification"]["conclusion"] == "success"
    assert attestation["package_qualification"]["job"] == (
        producer.QUALIFICATION_JOB_NAME
    )

    openssl = _openssl_executable()
    private_key, public_key = _ephemeral_signer(tmp_path)
    payload = producer.attestation_signing_payload(attestation)
    signature = producer.sign_ed25519(payload, private_key, openssl=openssl)
    producer.verify_ed25519(payload, signature, public_key, openssl=openssl)


def _ephemeral_signer(directory: Path) -> tuple[Path, Path]:
    """Mint an Ed25519 key inside the test; the production key is never used."""

    private = ed25519.Ed25519PrivateKey.generate()
    private_path = directory / "ephemeral-private.pem"
    public_path = directory / "ephemeral-public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


class _ProducerApi:
    """The Actions API surface the signer reads, and nothing else."""

    def __init__(self, *, archive: bytes, digest: str, release_tag: str) -> None:
        self.archive = archive
        self.digest = digest
        self.release_tag = release_tag
        self.downloads = 0
        self.run: dict[str, Any] = {
            "workflow_id": 77,
            "path": producer.WINDOWS_PACKAGE_WORKFLOW,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": release_tag,
            "head_repository": {"full_name": producer.PRODUCT_REPOSITORY},
            "head_sha": SOURCE_COMMIT,
            "run_attempt": 2,
        }
        self.jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 42,
                "head_sha": SOURCE_COMMIT,
            }
            for name in sorted(producer.REQUIRED_WINDOWS_JOB_NAMES)
        ]
        self.product_ci_jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 43,
                "head_sha": SOURCE_COMMIT,
            }
            for name in sorted(producer.REQUIRED_PRODUCT_CI_JOB_NAMES)
        ]
        # This producer run's own jobs, as the Actions API reports them.
        self.producer_jobs = [
            {
                "id": 7000 + offset,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 99,
                "run_attempt": 1,
                "head_sha": PRODUCER_SHA,
            }
            for offset, name in enumerate(sorted(producer.PRODUCER_JOB_NAMES))
        ]

    def get(self, path: str) -> Any:
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"type": "commit", "sha": PRODUCER_SHA}}
        if path.endswith("/actions/runs/42"):
            return self.run
        if path.endswith("/actions/workflows/77"):
            return {"id": 77, "path": producer.WINDOWS_PACKAGE_WORKFLOW}
        if path.endswith("/actions/workflows/ci.yml"):
            return {
                "id": 88,
                "path": producer.PRODUCT_CI_WORKFLOW,
                "state": "active",
            }
        if "/actions/workflows/88/runs?" in path:
            return {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 43,
                        "workflow_id": 88,
                        "path": producer.PRODUCT_CI_WORKFLOW,
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": producer.PRODUCT_DEFAULT_BRANCH,
                        "head_sha": SOURCE_COMMIT,
                        "run_attempt": 1,
                        "repository": {"full_name": producer.PRODUCT_REPOSITORY},
                        "head_repository": {
                            "full_name": producer.PRODUCT_REPOSITORY
                        },
                    }
                ],
            }
        if path.endswith("/actions/runs/43/attempts/1/jobs?per_page=100"):
            return {
                "total_count": len(self.product_ci_jobs),
                "jobs": self.product_ci_jobs,
            }
        if path.endswith("/actions/runs/42/attempts/2/jobs?per_page=100"):
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        if path.endswith("/actions/runs/99/attempts/1/jobs?per_page=100"):
            return {
                "total_count": len(self.producer_jobs),
                "jobs": self.producer_jobs,
            }
        if path.endswith("/actions/runs/42/artifacts?per_page=100"):
            artifacts = [
                {
                    "id": 90,
                    "name": f"windows-package-unsigned-{self.release_tag}",
                    "expired": False,
                    "digest": "sha256:" + "d" * 64,
                    "archive_download_url": "https://api.github.com/artifact/90",
                },
                {
                    "id": 91,
                    "name": f"windows-package-{self.release_tag}",
                    "expired": False,
                    "digest": self.digest,
                    "archive_download_url": "https://api.github.com/artifact/91",
                },
            ]
            return {"total_count": len(artifacts), "artifacts": artifacts}
        if "/git/ref/tags/" in path:
            return {"object": {"type": "commit", "sha": SOURCE_COMMIT}}
        if "/compare/" in path:
            return {"status": "ahead"}
        raise AssertionError(path)

    def download(self, path: str, destination: Path) -> None:
        assert urllib.parse.urlsplit(path).path.endswith("/91/zip"), path
        self.downloads += 1
        destination.write_bytes(self.archive)


def _signer_environment(base_url: str, temp_parent: Path) -> dict[str, str]:
    return _gate_environment(base_url) | {
        "GITHUB_TOKEN": "synthetic-product-token",
        "GITHUB_REF": f"refs/heads/{producer.PRODUCT_DEFAULT_BRANCH}",
        "GITHUB_SHA": PRODUCER_SHA,
        "PRODUCER_WORKFLOW_SHA": PRODUCER_SHA,
        "PRODUCER_CONTROL_BASELINE_SHA": BASELINE_SHA,
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "REVIEW_REPOSITORY": "review-owner/evidence-control",
        "REVIEW_DEFAULT_BRANCH": "trunk",
        "REVIEW_PROJECTION_PATH": "projection/review-state.json",
        "REVIEW_PROJECTION_DIGEST_PATH": "projection/review-state.sha256",
        "TMPDIR": str(temp_parent),
        "TEMP": str(temp_parent),
        "TMP": str(temp_parent),
    }


def test_the_qualify_package_command_runs_the_controlled_audit(
    packaged_release: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The audit's only production caller, driven through the CLI the job runs."""

    archive, digest = _actions_artifact(
        packaged_release["bytes"],
        filename=packaged_release["filename"],
        version=packaged_release["version"],
    )
    api = _ProducerApi(
        archive=archive, digest=digest, release_tag=packaged_release["release_tag"]
    )
    audited: list[Path] = []
    real_audit = producer.run_package_audit

    def recording_audit(**kwargs: Any) -> str:
        audited.append(kwargs["artifact"])
        return real_audit(**kwargs)

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(producer, "GitHubApi", lambda *_, **__: api)
    monkeypatch.setattr(producer, "run_package_audit", recording_audit)

    temp_parent = tmp_path / "qualify-temp"
    temp_parent.mkdir()
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        for name, value in _signer_environment(base_url, temp_parent).items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
        exit_code = producer.main(
            [
                "qualify-package",
                "--windows-package-run-id",
                "42",
                "--release-tag",
                packaged_release["release_tag"],
                "--legal-repository-revision",
                packaged_release["legal_source_revision"],
                "--legal-bindings-repository-revision",
                FETCH_REVISION,
                "--legal-bindings-path",
                LEGAL_DOCUMENT_PATH,
                "--legal-bindings-digest",
                packaged_release["legal_digest"],
            ]
        )

    assert exit_code == 0
    assert api.downloads == 1
    assert state.document_requests == 1
    assert [path.name for path in audited] == [packaged_release["filename"]]
    reported = capsys.readouterr().out
    assert hashlib.sha256(packaged_release["bytes"]).hexdigest() in reported


def _run_signer(
    packaged_release: dict[str, Any],
    api: "_ProducerApi",
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_revision: str,
) -> tuple[int, list[str], list[bytes], Path, int]:
    """Drive the real signing command, watching for key use and signatures."""

    decoded: list[str] = []
    signed: list[bytes] = []

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(producer, "GitHubApi", lambda *_, **__: api)
    monkeypatch.setattr(
        producer,
        "_decode_secret_to_file",
        lambda name, destination: decoded.append(name),
    )
    monkeypatch.setattr(
        producer,
        "sign_ed25519",
        lambda payload, key, **_: signed.append(payload) or b"signature",
    )

    temp_parent = tmp_path / "signer-temp"
    temp_parent.mkdir()
    with _legal_repository(
        packaged_release["legal_document"], packaged_release["legal_source_revision"]
    ) as (base_url, state):
        for name, value in _signer_environment(base_url, temp_parent).items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
        exit_code = producer.main(
            [
                "sign-and-handoff",
                "--windows-package-run-id",
                "42",
                "--release-tag",
                packaged_release["release_tag"],
                "--legal-repository-revision",
                source_revision,
                "--legal-bindings-repository-revision",
                FETCH_REVISION,
                "--legal-bindings-path",
                LEGAL_DOCUMENT_PATH,
                "--legal-bindings-digest",
                packaged_release["legal_digest"],
                "--legal-projection-source-commit",
                "e" * 40,
                "--legal-projection-canonical-sha256",
                "1" * 64,
            ]
        )
        requests = state.document_requests
    return exit_code, decoded, signed, temp_parent, requests


def test_a_failed_qualification_job_stops_the_signer(
    packaged_release: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every dispatch input is correct and the audit job says it failed.

    The signer reads that conclusion from the Actions API for its own run
    rather than from anything the unprivileged job handed it.
    """

    _openssl_executable()
    archive, digest = _actions_artifact(
        packaged_release["bytes"],
        filename=packaged_release["filename"],
        version=packaged_release["version"],
    )
    api = _ProducerApi(
        archive=archive, digest=digest, release_tag=packaged_release["release_tag"]
    )
    qualification = next(
        job
        for job in api.producer_jobs
        if job["name"] == producer.QUALIFICATION_JOB_NAME
    )
    qualification["conclusion"] = "failure"

    exit_code, decoded, signed, temp_parent, requests = _run_signer(
        packaged_release,
        api,
        tmp_path,
        monkeypatch,
        source_revision=packaged_release["legal_source_revision"],
    )

    assert exit_code == 15
    # The Legal chain ran to completion first, so the refusal is the
    # qualification conclusion and nothing earlier.
    assert requests == 1
    assert decoded == []
    assert signed == []
    assert sorted(temp_parent.rglob("*.json")) == []


def test_a_refused_precondition_produces_no_signed_attestation(
    packaged_release: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signer reaches the artifact and stops before any key material."""

    _openssl_executable()
    archive, digest = _actions_artifact(
        packaged_release["bytes"],
        filename=packaged_release["filename"],
        version=packaged_release["version"],
    )
    api = _ProducerApi(
        archive=archive, digest=digest, release_tag=packaged_release["release_tag"]
    )
    exit_code, decoded, signed, temp_parent, requests = _run_signer(
        packaged_release,
        api,
        tmp_path,
        monkeypatch,
        # A Legal approval revision the artifact was not packaged against, so
        # the chain refuses at its first step.
        source_revision="e" * 40,
    )

    assert exit_code == 10, "the refusal must name the coordinate step"
    # The signer got as far as the artifact itself, so the refusal is the
    # precondition refusing and not an earlier accident.
    assert api.downloads == 1
    assert requests == 0
    assert decoded == [], "key material was decoded past a refused precondition"
    assert signed == [], "a signature was produced past a refused precondition"
    staged = sorted(temp_parent.rglob("*.json"))
    assert staged == [], f"a refused run left staged evidence: {staged}"
